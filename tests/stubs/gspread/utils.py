"""Test stub for gspread.utils — only rowcol_to_a1, which sheets.py imports."""


def rowcol_to_a1(row: int, col: int) -> str:
    letters = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row}"
