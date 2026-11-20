import errno
import hashlib
import os
import stat
from pathlib import Path

CACHE_DIRECTORY = ".ai-translator-cache"
CACHE_FILENAME = "state.sqlite3"
PUBLICATION_DIRECTORY = ".ai-translator-publication"


def path_too_long(path: Path) -> bool:
    if os.name == "nt" and len(path.name.encode("utf-16-le", errors="surrogatepass")) > 510:
        return True
    try:
        path.lstat()
    except OSError as exc:
        return exc.errno == errno.ENAMETOOLONG or getattr(exc, "winerror", None) == 206
    except ValueError:
        # The caller reports invalid path syntax separately.
        return False
    return False


def default_output(source: Path, operation: str, *, chapter: int | None = None) -> Path:
    suffix = {
        "translate": f".ru{source.suffix.lower()}",
        "sample": f".chapter-{chapter}.sample.ru.txt" if chapter is not None else ".sample.ru.txt",
        "glossary": ".glossary.json",
    }[operation]
    destination = source.with_name(source.stem + suffix)
    if not path_too_long(destination):
        return destination
    digest = hashlib.sha256(os.fsencode(os.path.normcase(source.name))).hexdigest()[:16]
    shortened = source.with_name(f"{source.stem[:40]}.{digest}{suffix}")
    return source.with_name(f"translated-{digest}{suffix}") if path_too_long(shortened) else shortened


def private_artifact(root: Path, name: str, filename: str, *, create: bool = False) -> Path:
    if name in ("", ".", "..") or Path(name).name != name:
        raise ValueError("Некорректное имя служебной папки.")
    leaf = root / name
    for directory in (root, leaf):
        if create:
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
        try:
            info = directory.lstat()
        except FileNotFoundError:
            if create:
                raise
            break
        attributes = getattr(info, "st_file_attributes", 0)
        if not stat.S_ISDIR(info.st_mode) or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"Для служебных файлов требуется обычная папка. {directory}")
    return leaf / filename
