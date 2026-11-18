"""Package source code and measured grade-5 results, without the dataset or venv."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parent


def main():
    results = ROOT / "output/grade5/wyniki.md"
    if not results.exists():
        raise SystemExit("Run grade5.py all and export_results.py before packaging")
    paths = set()
    for pattern in ["*.py", "README*.md", "requirements*.txt", "docker-compose*.yml", ".gitignore", "Lab3.md", "Sprawozdanie3_*_LIKE.docx"]:
        paths.update(ROOT.glob(pattern))
    for directory in ["sql", "solr", "output/grade5", "output/pdf"]:
        paths.update(p for p in (ROOT / directory).rglob("*") if p.is_file())
    target = ROOT / "output/LAB3_czesc_5_0.zip"
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            archive.write(path, path.relative_to(ROOT).as_posix())
    with ZipFile(target) as archive:
        assert archive.testzip() is None
        assert not any(n.startswith(("data/", ".venv/", ".git/")) for n in archive.namelist())
    print(target)


if __name__ == "__main__":
    main()
