"""Test stub for gspread — only what sheets.py imports at module level."""
class Client:  # noqa
    pass

class Worksheet:  # noqa
    pass

class WorksheetNotFound(Exception):
    pass

def authorize(creds):
    raise NotImplementedError("stub gspread — patch sheets.* in tests")
