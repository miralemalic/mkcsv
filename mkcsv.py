#!/usr/bin/env python3
"""Convert data dumps to CSV: delimited text, SQL dumps, and console tables.

  mkcsv data.txt -o ~/Desktop                 # sniff delimiter
  mkcsv dump.sql -o ~/Desktop                 # INSERT and/or COPY dumps
  mkcsv query.txt -o ~/Desktop                # psql / mysql aligned output
  mkcsv data.txt -o ~/Desktop --split 200000  # chunk into 200k-row files
  mkcsv ~/data -o ~/Desktop                   # whole folder

The input format is detected from the file's contents, not its extension, so a
.sql file that is actually console output (or a .txt that is really SQL) still
works. Override with --format if a guess is ever wrong.

Formats understood:
  delimited  tab / pipe / semicolon / comma / colon separated text
  sql        MySQL-style INSERT dumps and PostgreSQL COPY ... FROM stdin blocks
             (CREATE TABLE is read for column names when present)
  aligned    psql tables ( a | b  with a ---+--- rule ), mysql +----+ boxes,
             and Markdown tables

With --split N, output is written as multiple files of at most N data rows
each, "<base> - part 001.csv", ..., header repeated in every part. Rows are
partitioned, never dropped or reordered.

Inputs are opened read-only and never modified. Everything streams, so memory
use stays flat regardless of input size. Standard library only.
"""

import argparse
import csv
import os
import re
import shutil
import sys
import time

__version__ = "1.0.0"

CHUNK = 1 << 22          # 4 MiB reads
PROGRESS_MIN = 50 << 20  # only show progress for inputs above 50 MiB
QUIET = False            # -q silences the per-file report, never the errors

# csv chokes on huge single fields otherwise (long TEXT/BLOB columns)
csv.field_size_limit(2**31 - 1)


def say(*args, **kwargs):
    """print() for progress chatter; silenced by -q. Errors bypass this."""
    if not QUIET:
        print(*args, **kwargs)


def free_space(path):
    """Bytes free at `path`, or None when the platform won't say."""
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


def enc_for_read(encoding):
    """Use utf-8-sig for utf-8 so a leading BOM is stripped transparently."""
    return "utf-8-sig" if encoding.lower().replace("-", "") in ("utf8",) else encoding


class Progress:
    """Single-line byte counter on stderr, silent for small inputs."""

    def __init__(self, label, total):
        self.total = total
        self.label = label
        self.on = total >= PROGRESS_MIN and sys.stderr.isatty() and not QUIET
        self.done = 0
        self.last = 0.0
        self.start = time.time()

    def advance(self, n):
        self.done += n
        if not self.on:
            return
        now = time.time()
        if now - self.last < 0.25:
            return
        self.last = now
        pct = 100.0 * self.done / self.total if self.total else 0.0
        rate = self.done / max(now - self.start, 1e-6)
        sys.stderr.write(
            f"\r    {self.label}: {pct:5.1f}%  {human(self.done)}/{human(self.total)}"
            f"  {human(rate)}/s   ")
        sys.stderr.flush()

    def close(self):
        if self.on:
            sys.stderr.write("\r" + " " * 78 + "\r")
            sys.stderr.flush()


class Out:
    """CSV sink for one logical table/file, with optional row splitting.

    split=0 writes a single "<base>.csv". Otherwise "<base> - part NNN.csv"
    files of at most `split` data rows each, repeating `header` in every part.
    header=None writes no header line. Output uses LF endings, which every
    upload/validation tool accepts and matches typical Linux data.
    """

    def __init__(self, outdir, base, header, split, force):
        self.outdir = outdir
        self.base = base
        self.header = header
        self.split = split
        self.force = force
        self.part = 0
        self.in_part = 0
        self.total = 0
        self.handle = None
        self.writer = None
        self.paths = []
        self.counts = []
        self._open_next()

    def _path(self):
        if self.split:
            return os.path.join(self.outdir, f"{self.base} - part {self.part:03d}.csv")
        return os.path.join(self.outdir, f"{self.base}.csv")

    def _open_next(self):
        if self.handle:
            self.counts.append(self.in_part)
            self.handle.close()
        self.part += 1
        path = self._path()
        if os.path.exists(path) and not self.force:
            raise FileExistsError(
                f"{os.path.basename(path)} already exists (use --force to overwrite)")
        self.handle = open(path, "w", newline="", encoding="utf-8")
        self.writer = csv.writer(self.handle, lineterminator="\n")
        if self.header is not None:
            self.writer.writerow(self.header)
        self.paths.append(path)
        self.in_part = 0

    def writerow(self, row):
        if self.split and self.in_part >= self.split:
            self._open_next()
        self.writer.writerow(row)
        self.in_part += 1
        self.total += 1

    def close(self):
        if self.handle:
            self.counts.append(self.in_part)
            self.handle.close()
            self.handle = None

    def report(self, src_name):
        """Print what each part got. Call after close().

        Counts come from what we wrote, not from re-reading the file: a
        quoted field may contain newlines, so counting lines in the output
        would over-report rows on exactly the data most likely to have them.
        """
        for path, n in zip(self.paths, self.counts):
            say(f"  {src_name} -> {os.path.basename(path)}"
                f"  ({n:,} rows, {human(os.path.getsize(path))})")


# ------------------------------------------------------------ format detect

def _is_divider(s):
    """A ruled line such as ---+---, +----+----+, ====, or | :--- | ---: |."""
    s = s.strip()
    if len(s) < 3:
        return False
    if set(s) - set("-+=|: "):
        return False
    return "-" in s or "=" in s


_FOOTER_RE = re.compile(r"^\(\d+\s+rows?\)$", re.IGNORECASE)


def detect_format(path, encoding):
    with open(path, encoding=enc_for_read(encoding), errors="replace") as fh:
        sample = fh.read(256 * 1024)

    if re.search(r"\bINSERT\s+INTO\b", sample, re.IGNORECASE):
        return "sql"
    if re.search(r"\bCOPY\b[^\n]*\bFROM\s+STDIN\b", sample, re.IGNORECASE):
        return "sql"

    lines = sample.splitlines()
    has_divider = any(_is_divider(ln) for ln in lines)
    has_pipe = any("|" in ln for ln in lines)
    has_footer = any(_FOOTER_RE.match(ln.strip()) for ln in lines)
    if has_divider and (has_pipe or has_footer):
        return "aligned"
    return "delimited"


# ---------------------------------------------------------------- delimited


def sniff_delimiter(sample):
    """Guess the delimiter of a delimited text file.

    csv.Sniffer alone is unreliable on files whose fields contain prose, so
    fall back to picking whichever candidate appears most consistently on
    every line.
    """
    try:
        return csv.Sniffer().sniff(sample, delimiters="\t|;,:").delimiter
    except csv.Error:
        pass

    lines = [ln for ln in sample.splitlines() if ln.strip()][:20]
    if not lines:
        return "\t"

    best, best_score = "\t", -1.0
    for cand in ("\t", "|", ";", ",", ":"):
        counts = [ln.count(cand) for ln in lines]
        if not counts or counts[0] == 0:
            continue
        consistent = all(c == counts[0] for c in counts)
        score = counts[0] * (2.0 if consistent else 0.5)
        if score > best_score:
            best, best_score = cand, score
    return best


def convert_delimited(path, outdir, delimiter, encoding, force, split, no_header, no_quote):
    size = os.path.getsize(path)
    prog = Progress(os.path.basename(path), size)

    def counted(handle):
        # fh.tell() is illegal while csv.reader iterates the file, so track
        # progress by counting the characters of each line we feed it.
        for line in handle:
            prog.advance(len(line))
            yield line

    quoting = csv.QUOTE_NONE if no_quote else csv.QUOTE_MINIMAL

    with open(path, "r", encoding=enc_for_read(encoding), errors="replace", newline="") as fh:
        sample = fh.read(256 * 1024)
        fh.seek(0)
        delim = delimiter or sniff_delimiter(sample)
        base = os.path.splitext(os.path.basename(path))[0]

        reader = csv.reader(counted(fh), delimiter=delim, quoting=quoting)

        header = None
        if split and not no_header:
            for row in reader:
                if row:
                    header = row
                    break

        out = Out(outdir, base, header, split, force)
        rows = 0
        widths = set()
        try:
            for row in reader:
                if not row:
                    continue
                out.writerow(row)
                widths.add(len(row))
                rows += 1
        finally:
            out.close()
    prog.close()

    shown = {"\t": "\\t"}.get(delim, delim)
    note = f"  [!] ragged: {min(widths)}-{max(widths)} cols" if len(widths) > 1 else ""
    say(f"  {os.path.basename(path)}: delimited '{shown}', {rows:,} data rows{note}")
    out.report(os.path.basename(path))
    return len(out.paths)


# ------------------------------------------------------------------ aligned

def _split_cells(raw, border):
    s = raw.strip()
    if border:
        s = s.strip("|")
    return [c.strip() for c in s.split("|")]


def convert_aligned(path, outdir, encoding, force, split):
    size = os.path.getsize(path)
    prog = Progress(os.path.basename(path), size)
    base = os.path.splitext(os.path.basename(path))[0]

    out = None
    header = None
    border = False
    width = 0
    rows = 0

    with open(path, "r", encoding=enc_for_read(encoding), errors="replace") as fh:
        try:
            for line in fh:
                prog.advance(len(line))
                raw = line.rstrip("\n").rstrip("\r")
                s = raw.strip()

                if not s or _is_divider(s) or _FOOTER_RE.match(s):
                    continue
                # Skip banner/noise lines that appear before a multi-column
                # header (they contain no column separator).
                if header is None and "|" not in raw:
                    continue

                if header is None:
                    border = raw.lstrip().startswith("|")
                    header = _split_cells(raw, border)
                    width = len(header)
                    out = Out(outdir, base, header, split, force)
                    continue

                cells = _split_cells(raw, border)
                if len(cells) < width:
                    cells += [""] * (width - len(cells))
                elif len(cells) > width:
                    cells = cells[:width]
                out.writerow(cells)
                rows += 1
        finally:
            if out:
                out.close()
    prog.close()

    if out is None:
        print(f"  {os.path.basename(path)}: no table rows found", file=sys.stderr)
        return 0
    say(f"  {os.path.basename(path)}: aligned table, {len(header)} cols, {rows:,} rows")
    out.report(os.path.basename(path))
    return len(out.paths)


# ---------------------------------------------------------------------- sql

_INTERESTING = re.compile(r"""['"`;]|--|\#|/\*""")
_QUOTE_JUMP = {
    "'": re.compile(r"['\\]"),
    '"': re.compile(r'["\\]'),
    "`": re.compile(r"`"),
}
_BARE_END = re.compile(r"""[,()'"`]""")
_ESCAPE_RE = re.compile(r"\\(.)", re.DOTALL)

_MYSQL_ESCAPES = {
    "0": "\0", "b": "\b", "n": "\n", "r": "\r",
    "t": "\t", "Z": "\x1a", "\\": "\\", "'": "'", '"': '"',
}
_PG_ESCAPES = {
    "t": "\t", "n": "\n", "r": "\r", "\\": "\\",
    "b": "\b", "f": "\f", "v": "\v",
}
_NOT_A_COLUMN = {
    "primary", "key", "unique", "constraint", "index", "foreign",
    "check", "fulltext", "spatial", "period",
}
# One identifier: bare word chars, or a `quoted` / "quoted" / [quoted] run,
# which may itself hold spaces and dots. Each branch consumes at least one
# character and they start with distinct characters, so there is no ambiguity
# for the regex engine to backtrack over.
_IDENT = r"(?:[\w.]|`[^`]*`|\"[^\"]*\"|\[[^\]]*\])+"

_COPY_RE = re.compile(
    r"COPY\s+(" + _IDENT + r")\s*(\(([^)]*)\))?\s+FROM\s+STDIN(.*)$",
    re.IGNORECASE | re.DOTALL,
)


def _scan_quoted(text, start):
    """Return the index just past the quoted run beginning at `start`, or None
    if the run is not terminated inside `text` (caller needs more input)."""
    quote = text[start]
    pat = _QUOTE_JUMP[quote]
    n = len(text)
    i = start + 1
    while i < n:
        m = pat.search(text, i)
        if not m:
            return None
        j = m.start()
        if text[j] == "\\":            # backslash escape; backticks never match
            i = j + 2
            continue
        if j + 1 < n and text[j + 1] == quote:
            i = j + 2                  # '' or "" is an escaped quote
            continue
        if j + 1 == n:
            return None                # can't yet tell whether a '' follows
        return j + 1
    return None


def _copy_header(stmt):
    """(table, columns_or_None, is_csv, delimiter) if stmt is COPY..FROM stdin."""
    m = _COPY_RE.match(stmt.strip())
    if not m:
        return None
    table = _table_name(m.group(1))
    cols = None
    if m.group(3) is not None:
        cols = [_strip_ident(c) for c in m.group(3).split(",") if c.strip()]
    opts = m.group(4) or ""
    is_csv = bool(re.search(r"\bCSV\b", opts, re.IGNORECASE))
    dm = re.search(r"DELIMITER\s+'([^'])'", opts, re.IGNORECASE)
    delim = dm.group(1) if dm else ("," if is_csv else "\t")
    return table, cols, is_csv, delim


def _pg_unescape(field):
    if "\\" not in field:
        return field
    return _ESCAPE_RE.sub(lambda m: _PG_ESCAPES.get(m.group(1), m.group(1)), field)


def _pg_copy_row(line, delim, is_csv):
    if is_csv:
        return next(csv.reader([line], delimiter=delim))
    out = []
    for f in line.split(delim):
        out.append("" if f == "\\N" else _pg_unescape(f))
    return out


def iter_sql(fh, prog=None):
    """Yield ('stmt', text) for SQL statements and ('copy', table, cols, csv,
    delim, line) for each PostgreSQL COPY data line."""
    buf = []
    pending = ""
    copy = None            # (table, cols, is_csv, delim) while inside a block
    copy_skip_first = False

    for chunk in iter(lambda: fh.read(CHUNK), ""):
        if prog:
            prog.advance(len(chunk))
        text = pending + chunk
        pending = ""
        i = 0
        n = len(text)

        while i < n:
            if copy is not None:
                nl = text.find("\n", i)
                if nl == -1:
                    pending = text[i:]
                    i = n
                    break
                line = text[i:nl]
                i = nl + 1
                if line.endswith("\r"):
                    line = line[:-1]
                if copy_skip_first and line == "":
                    copy_skip_first = False
                    continue
                copy_skip_first = False
                if line == "\\.":
                    copy = None
                    continue
                yield ("copy", copy[0], copy[1], copy[2], copy[3], line)
                continue

            m = _INTERESTING.search(text, i)
            if not m:
                if text.endswith(("-", "/")) and n - 1 >= i:
                    buf.append(text[i:-1])   # may be empty, which is fine
                    pending = text[-1:]
                else:
                    buf.append(text[i:])
                break

            tok = m.group(0)
            start = m.start()
            buf.append(text[i:start])

            if tok == ";":
                stmt = "".join(buf).strip()
                buf = []
                i = start + 1
                if stmt:
                    ch = _copy_header(stmt)
                    if ch:
                        copy = (ch[0], ch[1], ch[2], ch[3])
                        copy_skip_first = True
                    else:
                        yield ("stmt", stmt)
                continue

            if tok in ("--", "#"):
                nl = text.find("\n", start)
                if nl == -1:
                    pending = text[start:]
                    break
                buf.append(" ")
                i = nl + 1
                continue

            if tok == "/*":
                end = text.find("*/", start + 2)
                if end == -1:
                    pending = text[start:]
                    break
                buf.append(" ")
                i = end + 2
                continue

            end = _scan_quoted(text, start)
            if end is None:
                pending = text[start:]
                break
            buf.append(text[start:end])
            i = end

    if copy is not None:
        if pending and pending not in ("", "\\."):
            line = pending[:-1] if pending.endswith("\r") else pending
            if line not in ("", "\\."):
                yield ("copy", copy[0], copy[1], copy[2], copy[3], line)
    else:
        tail = ("".join(buf) + pending).strip()
        if tail and not _copy_header(tail):
            yield ("stmt", tail)


class _Tokens:
    """Minimal cursor over a statement, for reading VALUES tuples."""

    def __init__(self, text, pos=0, backslash_escapes=True):
        self.text = text
        self.pos = pos
        self.backslash_escapes = backslash_escapes

    def peek(self):
        text, n = self.text, len(self.text)
        i = self.pos
        while i < n and text[i].isspace():
            i += 1
        self.pos = i
        return text[i] if i < n else ""

    def take(self, ch):
        if self.peek() == ch:
            self.pos += 1
            return True
        return False

    def read_value(self):
        text, n = self.text, len(self.text)
        if self.peek() == "":
            return ""
        start = self.pos
        c = text[start]

        if c in "'\"":
            end = _scan_quoted(text, start)
            if end is None:
                end = n
                raw = text[start + 1:]
            else:
                raw = text[start + 1:end - 1]
            self.pos = end
            return self._unescape(raw, c)

        depth = 0
        while self.pos < n:
            m = _BARE_END.search(text, self.pos)
            if not m:
                self.pos = n
                break
            j = m.start()
            ch = text[j]
            if ch in "'\"`":
                nxt = _scan_quoted(text, j)
                self.pos = nxt if nxt else n
                continue
            if ch == "(":
                depth += 1
                self.pos = j + 1
                continue
            if ch == ")":
                if depth == 0:
                    self.pos = j
                    break
                depth -= 1
                self.pos = j + 1
                continue
            if depth == 0:
                self.pos = j
                break
            self.pos = j + 1

        word = text[start:self.pos].strip()
        return "" if word.upper() == "NULL" else word

    def _unescape(self, raw, quote):
        if quote * 2 in raw:
            raw = raw.replace(quote * 2, quote)
        if not self.backslash_escapes or "\\" not in raw:
            return raw
        return _ESCAPE_RE.sub(lambda m: _MYSQL_ESCAPES.get(m.group(1), m.group(1)), raw)


def _strip_ident(name):
    return name.strip().strip('`"[]').strip()


def _table_name(raw):
    """Last component of a possibly-qualified table name, unquoted.

    `db`.`t` -> t, public.leads -> leads, [dbo].[My Table] -> My Table.
    Splitting has to happen before unquoting, because a dot inside quotes is
    part of the name and a quote stripped first would hide where it ended.
    """
    parts, cur, i, n = [], [], 0, len(raw)
    while i < n:
        c = raw[i]
        if c in "`\"[":
            close = {"`": "`", '"': '"', "[": "]"}[c]
            j = raw.find(close, i + 1)
            if j == -1:                      # unterminated; take the rest
                cur.append(raw[i + 1:])
                break
            cur.append(raw[i + 1:j])
            i = j + 1
            continue
        if c == ".":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    parts.append("".join(cur))

    # the name reaches the filesystem as "<name>.csv"; never let a dump steer
    # that out of the output directory
    name = re.sub(r"[/\\\x00]", "_", parts[-1].strip()).strip(". ")
    return name or "table"


def _match_paren(text, start):
    depth, i, n = 0, start, len(text)
    while i < n:
        c = text[i]
        if c in "'\"`":
            nxt = _scan_quoted(text, i)
            i = nxt if nxt else n
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def parse_create_table(stmt):
    m = re.match(
        r"""CREATE\s+(?:TEMPORARY\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?
            (""" + _IDENT + r""")\s*\(""",
        stmt, re.IGNORECASE | re.VERBOSE,
    )
    if not m:
        return None
    table = _table_name(m.group(1))
    open_paren = m.end() - 1
    body = stmt[open_paren + 1:_match_paren(stmt, open_paren) - 1]

    parts, depth, cur = [], 0, []
    for c in body:
        if c == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        depth += (c == "(") - (c == ")")
        cur.append(c)
    parts.append("".join(cur))

    cols = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        first = part.split()[0]
        if _strip_ident(first).lower() in _NOT_A_COLUMN:
            continue
        cols.append(_strip_ident(first))
    return table, cols


_INSERT_RE = re.compile(
    r"""INSERT\s+(?:LOW_PRIORITY\s+|DELAYED\s+|HIGH_PRIORITY\s+)?
        (?:IGNORE\s+)?INTO\s+(""" + _IDENT + r""")\s*""",
    re.IGNORECASE | re.VERBOSE,
)


def parse_insert(stmt, backslash_escapes):
    m = _INSERT_RE.match(stmt)
    if not m:
        return None
    table = _table_name(m.group(1))
    rest = stmt[m.end():]

    columns = None
    stripped = rest.lstrip()
    if stripped.startswith("("):
        close = _match_paren(stripped, 0)
        if re.match(r"\s*VALUES", stripped[close:], re.IGNORECASE):
            columns = [_strip_ident(c) for c in stripped[1:close - 1].split(",")]
            rest = stripped[close:]

    vm = re.search(r"\bVALUES?\b", rest, re.IGNORECASE)
    if not vm:
        return None

    def rows():
        cur = _Tokens(rest, vm.end(), backslash_escapes)
        while cur.take("("):
            row = []
            while True:
                row.append(cur.read_value())
                if cur.take(","):
                    continue
                cur.take(")")
                break
            yield row
            if not cur.take(","):
                break

    return table, columns, rows()


def convert_sql(path, outdir, encoding, backslash_escapes, force, split):
    schema = {}      # table -> columns from CREATE TABLE
    outs = {}        # table -> Out
    widths = {}      # table -> expected column count

    def sink(table, columns_hint, sample_len):
        if table not in outs:
            header = columns_hint or schema.get(table) or [
                f"col{i + 1}" for i in range(sample_len)]
            outs[table] = Out(outdir, table, header, split, force)
            widths[table] = len(header)
        return outs[table], widths[table]

    def emit(out, width, row):
        if len(row) < width:
            row = row + [""] * (width - len(row))
        elif len(row) > width:
            row = row[:width]
        out.writerow(row)

    size = os.path.getsize(path)
    prog = Progress(os.path.basename(path), size)

    try:
        with open(path, "r", encoding=enc_for_read(encoding), errors="replace") as fh:
            for item in iter_sql(fh, prog):
                if item[0] == "copy":
                    _, table, cols, is_csv, delim, line = item
                    row = _pg_copy_row(line, delim, is_csv)
                    out, width = sink(table, cols, len(row))
                    emit(out, width, row)
                    continue

                stmt = item[1]
                head = stmt[:12].upper()
                if head.startswith("CREATE"):
                    got = parse_create_table(stmt)
                    if got and got[1]:
                        schema[got[0]] = got[1]
                    continue
                if not head.startswith("INSERT"):
                    continue
                got = parse_insert(stmt, backslash_escapes)
                if not got:
                    continue
                table, columns, rows = got
                for row in rows:
                    out, width = sink(table, columns, len(row))
                    emit(out, width, row)
    finally:
        for out in outs.values():
            out.close()
        prog.close()

    if not outs:
        print(f"  {os.path.basename(path)}: no INSERT or COPY data found", file=sys.stderr)
        return 0

    for table, out in outs.items():
        say(f"  {os.path.basename(path)}: table '{table}', {out.total:,} rows"
            f" across {len(out.paths)} file(s)")
        out.report(os.path.basename(path))
    return sum(len(o.paths) for o in outs.values())


# ---------------------------------------------------------------------- cli


def main():
    ap = argparse.ArgumentParser(
        description="Convert delimited text, SQL dumps, and console tables to CSV. "
                    "Inputs are opened read-only and never modified.")
    ap.add_argument("inputs", nargs="+", help="files or directories")
    ap.add_argument("-o", "--outdir", help="output directory (default: next to each input)")
    ap.add_argument("-f", "--format", choices=["auto", "delimited", "sql", "aligned"],
                    default="auto", help="input format (default: auto-detect from content)")
    ap.add_argument("-d", "--delimiter",
                    help="delimiter for text files, e.g. -d '|' or -d tab (default: sniff)")
    ap.add_argument("-e", "--encoding", default="utf-8", help="input encoding (default: utf-8)")
    ap.add_argument("-s", "--split", type=int, default=0, metavar="N",
                    help="split output into files of at most N data rows each")
    ap.add_argument("--no-header", action="store_true",
                    help="delimited input has no header row (only affects --split)")
    ap.add_argument("--no-quote", action="store_true",
                    help="delimited input is unquoted; treat \" literally (avoids "
                         "merging rows when a value contains a stray quote)")
    ap.add_argument("--no-backslash-escapes", action="store_true",
                    help="treat backslashes in SQL strings literally (some Postgres dumps)")
    ap.add_argument("--force", action="store_true", help="overwrite existing output files")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="report only errors")
    ap.add_argument("--version", action="version", version=f"mkcsv {__version__}")
    args = ap.parse_args()

    global QUIET
    QUIET = args.quiet

    if args.split < 0:
        print("--split must be a positive number of rows", file=sys.stderr)
        return 2

    delim = args.delimiter
    if delim in ("tab", "\\t"):
        delim = "\t"

    paths = []
    for item in args.inputs:
        item = os.path.expanduser(item)
        if os.path.isdir(item):
            for name in sorted(os.listdir(item)):
                if name.lower().endswith((".txt", ".sql", ".tsv", ".dat", ".csv")):
                    paths.append(os.path.join(item, name))
        else:
            paths.append(item)

    if not paths:
        print("no matching input files", file=sys.stderr)
        return 1

    outdir_arg = os.path.expanduser(args.outdir) if args.outdir else None
    if outdir_arg:
        os.makedirs(outdir_arg, exist_ok=True)

    total_in = sum(os.path.getsize(p) for p in paths if os.path.isfile(p))
    check_dir = outdir_arg or os.path.dirname(os.path.abspath(paths[0]))
    free = free_space(check_dir)
    say(f"converting {len(paths)} file(s), {human(total_in)} total"
        + (f", splitting at {args.split:,} rows/file" if args.split else ""))
    if free is not None:
        say(f"free space at destination: {human(free)}")
        if free < total_in:
            print("  [!] destination has less free space than the inputs; "
                  "CSV output is usually a similar size", file=sys.stderr)
    say()

    written, failed = 0, 0
    for path in paths:
        if not os.path.isfile(path):
            print(f"  {path}: not found", file=sys.stderr)
            failed += 1
            continue

        outdir = outdir_arg or os.path.dirname(os.path.abspath(path))
        try:
            fmt = args.format
            if fmt == "auto":
                fmt = detect_format(path, args.encoding)
            say(f"[{os.path.basename(path)}] format: {fmt}")

            if fmt == "sql":
                written += convert_sql(path, outdir, args.encoding,
                                       not args.no_backslash_escapes, args.force, args.split)
            elif fmt == "aligned":
                written += convert_aligned(path, outdir, args.encoding, args.force, args.split)
            else:
                written += convert_delimited(path, outdir, delim, args.encoding,
                                             args.force, args.split, args.no_header,
                                             args.no_quote)
        except Exception as exc:              # keep going through a batch
            print(f"  {os.path.basename(path)}: FAILED ({exc})", file=sys.stderr)
            failed += 1

    say(f"\n{written} CSV file(s) written" + (f", {failed} failed" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
