"""Twenty tiny strings for the notebook's four five-page workers; no PDF files."""

PAGE_01 = "colour | Colour | F01\nNote: N01"
PAGE_02 = "size | Size | F02\nNote: N02"
PAGE_03 = "colour | Colour | F03\nNote: N03\nCarry: C03"
PAGE_04 = "local_1 | Local 1 | F04\nNote: N04"
PAGE_05 = "size | Size | F05\nNote: N05"
PAGE_06 = "colour | Colour | F06\nNote: N06"
PAGE_07 = "size | Size | F07\nNote: N07"
PAGE_08 = "colour | Colour | F08\nNote: N08\nCarry: C08"
PAGE_09 = "local_2 | Local 2 | F09\nNote: N09"
PAGE_10 = "size | Size | F10\nNote: N10"
PAGE_11 = "colour | Colour | F11\nNote: N11"
PAGE_12 = "size | Size | F12\nNote: N12"
PAGE_13 = "colour | Colour | F13\nNote: N13\nCarry: C13"
PAGE_14 = "local_3 | Local 3 | F14\nNote: N14"
PAGE_15 = "size | Size | F15\nNote: N15"
PAGE_16 = "colour | Colour | F16\nNote: N16"
PAGE_17 = "size | Size | F17\nNote: N17"
PAGE_18 = "colour | Colour | F18\nNote: N18\nCarry: C18"
PAGE_19 = "local_4 | Local 4 | F19\nNote: N19"
PAGE_20 = "size | Size | F20\nNote: N20"

PAGE_TEXTS = [PAGE_01, PAGE_02, PAGE_03, PAGE_04, PAGE_05, PAGE_06, PAGE_07,
              PAGE_08, PAGE_09, PAGE_10, PAGE_11, PAGE_12, PAGE_13, PAGE_14,
              PAGE_15, PAGE_16, PAGE_17, PAGE_18, PAGE_19, PAGE_20]
PAGES = [{'page': number, 'markdown': text, 'image_count': 0,
          'extracted_text_empty': False} for number, text in enumerate(PAGE_TEXTS, 1)]
DOCUMENT = {'page_count': len(PAGES), 'pages': PAGES}

# The response contracts and table columns are imported unchanged from production.
READ_PROMPT = """You are pdf_codebook_agent in a tiny string-based checkpoint test.
Catalogue only current_page. Neighbour pages are context, not additional target pages.
Recompute every output field from current_page.markdown. Ignore older replies when copying.
IMPORTANT: If current_page.markdown has no 'Carry: ' line, unfinished_entry MUST be the
empty string, even if the incoming payload's unfinished_entry or earlier pages contain Cxx.
The first line is variable_name | variable_label | variable_description. Copy each field
exactly, stripping surrounding spaces. Return one row, lowercasing variable_name.
other_information and values are 'not in source'; source_presence is 'in source';
source_location is 'Page N' using current_page.page. general_information is the text
after 'Note: ' as a one-item list. unfinished_entry is the text after 'Carry: ', or ''.
issues is []. Do not add earlier rows from your conversation. Return the supplied JSON
contract. This is the same current-page/lookahead/unfinished-entry payload as production.
"""

MERGE_RULES = """Return one complete row for each supplied variable name and no others.
For each field, preserve every distinct supplied line in source order, joining with
newlines. Exact duplicate lines appear once. 'not in source' is absence: omit it if a
real value exists, otherwise retain it. Never paraphrase the Fxx facts or Page N locations.
Keep source_presence 'in source'. Return strict JSON using the supplied contract.
"""
MERGE_PROMPT = ("You are pdf_codebook_merge_agent. Merge existing_rows followed by "
                "current_page_rows, separately for each variable. " + MERGE_RULES)
RECONCILE_PROMPT = ("You are pdf_codebook_reconcile_agent. For each target_rows item, "
                    "merge its source_rows in their supplied order. " + MERGE_RULES)
