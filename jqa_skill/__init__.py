"""Source/wheel compatible console entrypoint."""
def main():
    import sys
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    if not (root/'scripts/public_cli.py').is_file():root=Path(__file__).resolve().parent
    sys.path.insert(0,str(root))
    from scripts.public_cli import entry
    entry()
