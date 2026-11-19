import io
import pandas as pd
import pyreadstat
import random
from markitdown import MarkItDown
from pathlib import Path
from pypdf import PdfReader, PdfWriter
from v_5.app.config import FORMATS


def sample_positions(size, first, extra):
    """Choose a repeatable mix of first and later positions.

    Inputs: size: total items; first: leading items to keep; extra: later sample size.
    Returns: List of zero-based positions, with sampled positions sorted.
    Details: Used by table/PDF readers. A fixed random seed makes repeated previews
        comparable.
    """
    head = list(range(min(size, first)))
    rest = range(len(head), size)
    return head + sorted(random.Random(17).sample(rest, min(extra, len(rest))))


def read_text(path, first=30, random_count=30, encoding="utf-8-sig", max_chars=None):
    """Preview a text file while counting all its lines.

    Inputs: path: source file; first/random_count: leading and later line counts; encoding:
        text decoder; max_chars: optional per-line character limit.
    Returns: Dict with line_count and lines containing one-based line numbers and text.
        A line cut at max_chars also has clipped_chars, the number of characters omitted.
    Details: Used by file reviewers, directly and through read_delimited/read_layout. Streams
        the file and keeps a fixed-size random sample instead of retaining every line.
    """
    head, reservoir, count = [], [], 0
    rng = random.Random(17)
    with open(path, encoding=encoding, errors="replace") as source:
        for count, line in enumerate(source, 1):
            item = {"line": count, "text": line.rstrip("\r\n")}
            if max_chars and len(item["text"]) > max_chars:
                item.update(text=item["text"][:max_chars], clipped_chars=len(item["text"]) - max_chars)
            if count <= first:
                head.append(item)
            elif len(reservoir) < random_count:
                reservoir.append(item)
            else:
                # Each later line gets a chance to replace a sample slot, without storing the whole file.
                position = rng.randrange(count - first)
                if position < random_count:
                    reservoir[position] = item
    return {"line_count": count, "lines": head + sorted(reservoir, key=lambda x: x["line"])}


def read_delimited(path, first=10, random_count=10, max_chars=2000, encoding="utf-8-sig"):
    """Preview a CSV/TSV file as raw numbered lines, without parsing it.

    Inputs: path: source file; first/random_count: leading and later line counts;
        max_chars: per-line character limit; encoding: text decoder.
    Returns: Text-preview dict with line_count and numbered lines; long lines carry
        clipped_chars.
    Details: Used for CSV/TSV file review. No delimiter, quoting, header or type rules are
        applied, so the reviewer sees the file as written. The line limit keeps very wide
        files within a reviewer's context.
    """
    return read_text(path, first, random_count, encoding, max_chars)


def read_layout(path, first=30, random_count=30, encoding="utf-8-sig"):
    """Preview a layout file as text.

    Inputs: path, first, random_count and encoding: forwarded to read_text.
    Returns: Text-preview dict with line_count and numbered lines.
    Details: Used for DCF/DCT file review; a layout is evidence to read, never a script to
        execute.
    """
    # A layout is inspected as text, never executed.
    return read_text(path, first, random_count, encoding)


def read_pdf(path, first=3, random_count=5):
    """Extract marked-up text from selected PDF pages.

    Inputs: path: PDF; first/random_count: leading and later page sample sizes.
    Returns: Dict with page_count and pages; each page has its number, markdown, image_count
        and empty-text flag.
    Details: File review samples pages; codebook extraction requests all allowed pages.
        Converts pages separately so source locations remain clear; does not perform OCR.
    """
    pdf = PdfReader(path)
    selected = sample_positions(len(pdf.pages), first, random_count)
    converter, pages = MarkItDown(enable_plugins=False), []
    for number in selected:
        stream, writer = io.BytesIO(), PdfWriter()
        writer.add_page(pdf.pages[number])
        writer.write(stream)
        stream.seek(0)
        text = converter.convert_stream(stream, file_extension=".pdf").text_content
        marker = f"-------- page {number + 1} -------"
        pages.append({"page": number + 1, "markdown": f"{marker}\n{text}\n{marker}",
                      "image_count": len(pdf.pages[number].images), "extracted_text_empty": not text.strip()})
    return {"page_count": len(pdf.pages), "pages": pages}


def read_statistical(path, first=10, random_count=10, rows=5):
    """Preview statistical data and summarize selected columns.

    Inputs: path: SPSS/Stata file; first/random_count: column selection; rows: preview row
        limit.
    Returns: (frame, metadata): sampled values plus full row/column counts and a
        sampled_databook of definitions, missingness and observed ranges.
    Details: Used by read_table. Scans all rows only for selected columns, excluding
        declared missing codes/ranges from statistics while retaining stored date values.
    """
    reader = {".sav": pyreadstat.read_sav, ".zsav": pyreadstat.read_sav,
              ".por": pyreadstat.read_por, ".dta": pyreadstat.read_dta}[Path(path).suffix.lower()]
    options = {"disable_datetime_conversion": True}
    if Path(path).suffix.lower() != ".por":
        options["user_missing"] = True
    _, meta = reader(path, metadataonly=True, **options)
    columns = meta.column_names
    selected = [columns[i] for i in sample_positions(len(columns), first, random_count)]
    frame, _ = reader(path, usecols=selected, row_limit=rows, **options)
    dictionary, distinct = {}, {}
    for name in selected:
        labels = meta.variable_value_labels.get(name, {})
        character = meta.readstat_variable_types.get(name) == "string"
        dictionary[name] = {
            "variable_name": name, "meaning": meta.column_names_to_labels.get(name),
            "type": "character" if character else "continuous" if meta.variable_measure.get(name) == "scale" else "categorical" if labels else "numeric",
            "storage_type": meta.readstat_variable_types.get(name),
            "measurement_level": meta.variable_measure.get(name),
            "original_format": meta.original_variable_types.get(name),
            "categories": [{"code": code, "meaning": label} for code, label in labels.items()],
            "missing_ranges": meta.missing_ranges.get(name, []),
            "missing_codes": meta.missing_user_values.get(name, []),
            "valid_count": 0, "missing_count": 0,
        }
        if character:
            distinct[name] = set()
        elif dictionary[name]["type"] in {"numeric", "continuous"}:
            dictionary[name].update(observed_min=None, observed_max=None)
    row_count = 0
    for batch, _ in pyreadstat.read_file_in_chunks(reader, path, chunksize=5000,
                                                  usecols=selected, **options):
        row_count += len(batch)
        for name in selected:
            info = dictionary[name]
            # Count both system nulls and the file's declared missing codes as missing, not as observed values.
            values = batch[name].dropna()
            values = values[~values.isin(info["missing_codes"])]
            for interval in info["missing_ranges"]:
                values = values[~values.between(interval["lo"], interval["hi"])]
            info["valid_count"] += len(values)
            info["missing_count"] += len(batch) - len(values)
            if info["type"] == "character":
                distinct[name].update(values.tolist())
            elif info["type"] in {"numeric", "continuous"} and len(values):
                low, high = float(values.min()), float(values.max())
                info["observed_min"] = low if info["observed_min"] is None else min(info["observed_min"], low)
                info["observed_max"] = high if info["observed_max"] is None else max(info["observed_max"], high)
    for name, values in distinct.items():
        dictionary[name]["unique_value_count"] = len(values)
    return frame, {
        "row_count": row_count, "column_count": len(columns),
        "sampled_databook": list(dictionary.values()),
        "statistics_scope": "All rows, sampled columns only. System and declared user-missing values excluded. Undeclared sentinels remain values; blank strings count unless declared missing.",
        "dictionary_scope": "Embedded definitions; category labels do not establish which codes occur in this data. Numeric without labels does not establish a continuous measure. Dates retain stored numeric units and original formats.",
    }


def excel_shapes(path):
    """Measure the occupied rectangle of each workbook sheet.

    Inputs: path: XLS or XLSX workbook.
    Returns: List of sheet, row_count and column_count dicts; the first row is treated as a
        header.
    Details: Used by read_table. Internal blank rows count, while trailing empty rows do not
        expand the reported data shape.
    """
    # Find the last populated row, retaining blank rows inside the data rectangle.
    if Path(path).suffix.lower() == ".xls":
        import xlrd
        book = xlrd.open_workbook(path, on_demand=True)
        shapes = [{"sheet": sheet.name, "row_count": max(0, sheet.nrows - 1),
                   "column_count": sheet.ncols} for sheet in book.sheets()]
        book.release_resources()
        return shapes
    from openpyxl import load_workbook
    book = load_workbook(path, read_only=True, data_only=True)
    shapes = []
    for sheet in book:
        last_row, last_column = 0, 0
        for number, values in enumerate(sheet.iter_rows(values_only=True), 1):
            occupied = [i for i, value in enumerate(values, 1) if value is not None]
            if occupied:
                last_row, last_column = number, max(last_column, max(occupied))
        shapes.append({"sheet": sheet.title, "row_count": max(0, last_row - 1),
                       "column_count": last_column})
    book.close()
    return shapes


def read_table(path, first=10, random_count=10, rows=5,
               delimiter=",", encoding="utf-8-sig", sheets=6):
    """Build a bounded preview for a tabular upload.

    Inputs: path: source; first/random_count: sampled columns; rows: preview limit;
        delimiter/encoding: text parsing; sheets: number of leading workbook sheets to preview.
    Returns: Dict of dimensions, sampled_types and string-valued sample rows, plus
        format-specific metadata. Excel returns sheet_previews for the first sheets and
        other_sheets with only the name and shape of the rest.
    Details: File review uses it for Excel and SPSS/Stata; CSV/TSV review uses read_delimited,
        so the CSV branch is no longer called there. Counts full records separately from the
        small preview so shape and sample are not confused.
    """
    fmt = FORMATS[Path(path).suffix.lower()]
    metadata = {}
    if fmt in {"csv", "tsv"}:
        columns = pd.read_csv(path, sep=delimiter, encoding=encoding, nrows=0).columns.tolist()
        selected = [columns[i] for i in sample_positions(len(columns), first, random_count)]
        frame = pd.read_csv(path, sep=delimiter, encoding=encoding, usecols=selected, nrows=rows, dtype=str)
        metadata["row_count"] = sum(len(batch) for batch in pd.read_csv(
            path, sep=delimiter, encoding=encoding, usecols=[columns[0]], chunksize=5000, dtype=str))
        metadata["shape_basis"] = "Parsed data records, excluding the header and blank lines; quoted multiline records count once."
    elif fmt == "excel":
        shapes = {shape["sheet"]: shape for shape in excel_shapes(path)}
        def shape(name):
            return {"sheet": name, **{key: shapes.get(name, {}).get(key) for key in ("row_count", "column_count")}}
        previews = []
        with pd.ExcelFile(path) as book:
            names = book.sheet_names
            for name in names[:sheets]:
                preview = shape(name)
                # One unreadable sheet (for example a chart sheet) must not hide the others.
                try:
                    columns = pd.read_excel(book, sheet_name=name, nrows=0).columns.tolist()
                    frame = pd.read_excel(book, sheet_name=name, nrows=rows, dtype=str,
                        usecols=sample_positions(len(columns), first, random_count)) if columns else pd.DataFrame()
                    preview.update(preview_rows=len(frame), sample=frame.astype(str).to_dict(orient="records"))
                except Exception as error:
                    preview["read_error"] = str(error)
                previews.append(preview)
        return {"sheet_count": len(names), "sheet_previews": previews,
                "other_sheets": [shape(name) for name in names[sheets:]],
                "shape_basis": "First row of each sheet is the header; rows through the last populated row, including internal blank rows."}
    else:
        frame, metadata = read_statistical(path, first, random_count, rows)
        columns = range(metadata["column_count"])
    return {"column_count": len(columns), "preview_rows": len(frame),
            "sampled_types": {str(k): str(v) for k, v in frame.dtypes.items()},
            "sample": frame.astype(str).to_dict(orient="records"), **metadata}
