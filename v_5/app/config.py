from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path(os.environ.get("V5_RUNTIME", ROOT / "app" / "runtime")).resolve()
MODEL = "gpt-6.2-sol"
FORMATTER_MODEL = "gpt-6-luna"
LOADER_SUMMARIZER_MODEL = "gpt-6-luna"
CODEBOOK_PDF_MODEL = os.environ.get('V5_CODEBOOK_PDF_MODEL', 'gpt-6-luna')
# US dollars per million tokens: (input, cached input, output). Output includes reasoning.
PRICES = {"gpt-6.2-sol": (2.00, 0.10, 10.00), "gpt-6-luna": (0.10, 0.01, 0.50)}
IMAGE = "atlas-v5-sandbox:local"
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
FORMATS = {
    ".pdf": "pdf", ".md": "md", ".txt": "txt", ".dcf": "dcf",
    ".dct": "dct", ".dta": "dta", ".sav": "spss", ".zsav": "spss",
    ".por": "spss", ".csv": "csv", ".tsv": "tsv", ".xlsx": "excel",
    ".xls": "excel", ".dat": "dat", ".map": "map",
}


def api_key():
    """Find the configured OpenAI API key.

    Inputs: Reads OPENAI_API_KEY, then V5_KEYS_FILE or the repository keys.txt fallback.
    Returns: Key string; raises if neither source supplies a key.
    Details: Used when constructing model clients. File-based values are trimmed and
        unquoted; the environment value is returned directly.
    """
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    path = Path(os.environ.get("V5_KEYS_FILE", ROOT.parent / "keys.txt"))
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "OPENAI_API_KEY":
                return value.strip().strip("\"'")
    raise RuntimeError("Set OPENAI_API_KEY or add it to the existing keys.txt.")
