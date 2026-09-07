# mkcsv

Point it at a `.sql`, `.txt`, `.tsv` or `.dat`. Get a CSV back.

SQL dumps. Console output someone pasted into a text file. Delimited text with
no obvious delimiter. mkcsv reads the file, works out what it is and writes
clean .csv/splits it into multiple .csv files.

One file. No dependencies. Runs on Python 3.9 or newer. Everything streams, so a
40 GB dump costs the same memory as a 40 KB one.

```bash
mkcsv dump.sql -o ~/Desktop
```

## The problem

Nobody sends you clean data.

You get a mysqldump. You get a psql table pasted into a `.txt`. You get a "CSV"
that turns out to be pipe-delimited with stray quotes in it. You get a 286 MB
export that Excel won't open and LibreOffice chokes on.

Each one needs a different trick. I got tired of remembering them.

## Install

```bash
curl -O https://raw.githubusercontent.com/miralemalic/mkcsv/main/mkcsv.py
chmod +x mkcsv.py
mv mkcsv.py ~/.local/bin/mkcsv
```

No pip or virtualenv. If Python 3.9 is on the box, it will run.

## Use

```bash
mkcsv data.txt                   # convert next to the input
mkcsv dump.sql -o ~/Desktop      # write somewhere else
mkcsv ~/exports -o ~/Desktop     # take the whole folder
mkcsv big.txt --split 200000     # cut into 200k-row files
mkcsv data.txt -d '|'            # force the delimiter
mkcsv weird.txt --format sql     # force the format
```

## What it takes

Three families. The format is read out of the file's contents. The extension is
ignored.

**Delimited text.** Tab, pipe, semicolon, comma, colon. `.txt`, `.tsv`, `.dat`,
and `.csv` itself. The delimiter is sniffed and printed on every run so you can
see what it decided. Quoted fields, newlines inside fields and byte-order marks
are handled. A pipe-delimited file called `.csv` comes out as a real CSV.

**SQL dumps.** `INSERT INTO` statements from mysqldump. `COPY ... FROM stdin`
blocks from pg_dump. Both in the same file, if that's what you were handed.
`CREATE TABLE` is read for column names when it's there. Every table in the dump
gets its own CSV. `NULL` becomes empty. Escapes and semicolons inside string
literals are handled by a real parser, not a regex.

**Console output.** The table your database client printed, pasted into a file.
psql (`---+---` rules, `(2 rows)` footers), the MySQL client (`+----+` boxes),
and Markdown tables.

## It reads what the file is, not what the extension says the file is

```
$ mkcsv query.sql
[query.sql] format: aligned
```

That file was named `.sql`. Its contents were pasted console output, so it was
converted as a table instead of parsed as SQL. Works the other way too: a `.txt`
full of `INSERT` statements is handled as SQL.

## Console output goes straight in

```
 id |      email       |   city
----+------------------+----------
  1 | ada@example.com  | London
  2 | alan@example.org | Wilmslow
(2 rows)
```

Comes out as:

```csv
id,email,city
1,ada@example.com,London
2,alan@example.org,Wilmslow
```

## Big files come out in pieces

```
$ mkcsv leads.txt -o ~/Desktop --split 50000 
(or 1 for all I care)
converting 1 file(s), 1.4GB total, splitting at 50,000 rows/file
free space at destination: 240.1GB

[leads.txt] format: delimited
  leads.txt: delimited '\t', 812,455 data rows
  leads.txt -> leads - part 001.csv  (50,000 rows, 88.1MB)
  leads.txt -> leads - part 002.csv  (50,000 rows, 87.9MB)
  ...
```

Every part repeats the header. Rows are partitioned. Nothing is dropped or reordered.

## Options

| Flag | Effect |
|---|---|
| `-o, --outdir DIR` | where to write. Default: beside each input |
| `-f, --format` | `auto`, `delimited`, `sql`, `aligned`. Default: auto |
| `-d, --delimiter` | force the delimiter, e.g. `-d '\|'` or `-d tab` |
| `-e, --encoding` | input encoding. Default: `utf-8`, BOM stripped |
| `-s, --split N` | cut output into files of at most N data rows |
| `--no-header` | input has no header row. Affects `--split` |
| `--no-quote` | treat `"` literally, for unquoted data with stray quotes |
| `--no-backslash-escapes` | treat `\` in SQL strings literally |
| `--force` | overwrite existing output |
| `-q, --quiet` | report only errors |
| `--version` | print the version |

## Rules

- Inputs are opened read-only. Nothing is ever written back to them.
- Nothing is loaded whole. Input size does not affect memory use.
- Existing output is never overwritten without `--force`.
- One bad file does not kill a batch. The error goes to stderr, the run carries
  on, the exit code comes back non-zero.
- Errors always print. `-q` silences the report, never the failures.

## Known limits

- MySQL and PostgreSQL dialects. SQL Server and Oracle exports are untested.
- No compressed input. Decompress it first.
- No stdin. Files and folders only.
- No Excel, no JSON. Text in, CSV out.
- Aligned tables need a `|` between columns. Columns held apart by spaces alone
  will not parse.
- Ragged rows pass through as they are, not padded. The column range is printed
  so you know it happened.
- Delimiter sniffing is a guess. It tells you what it guessed. If it guessed
  wrong, use `-d`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

60 tests. Standard library only, so there is nothing to install to run them
either.

One of them is `test_identical_output_at_every_chunk_size`. It pushes the same
SQL dump through read buffers from 1 byte up to 4 MB and demands byte-identical
output every time. That drags a buffer boundary across every offset in the file
and forces quotes, comments and statements to split mid-token. It found two
bugs the first time I ran it.

## License

MIT. See [LICENSE](LICENSE). Do what you want with it. Don't blame me.
