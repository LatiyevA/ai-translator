"""Recoverable publication of a book and its optional companion glossary."""

import errno
import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Callable, Iterable
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from .paths import PUBLICATION_DIRECTORY, path_too_long, private_artifact

_JOURNAL_NAME = re.compile(r"\.ai-translator-transaction-([0-9a-f]{32})\.json")
_BACKUP_DIGEST = re.compile(r"(?:file|link):[0-9a-f]{64}")
_MAX_METADATA = 4 * 1024 * 1024


@contextmanager
def exclusive_lock(path: Path, message: str):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError(message) from exc
        yield


def output_path(path: Path) -> Path:
    return path.parent.resolve() / path.name


def _sidecar(path: Path, suffix: str, *, create: bool = False) -> Path:
    legacy = path.with_name(f".{path.name}.ai-translator-publish.{suffix}")
    if not path_too_long(legacy):
        return legacy
    return private_artifact(path.parent / PUBLICATION_DIRECTORY, path.name, f"publish.{suffix}", create=create)


def _marker(path: Path, *, create: bool = False) -> Path:
    return _sidecar(path, "json", create=create)


def is_publication_sidecar(path: Path) -> bool:
    name = path.name.casefold()
    return any(part.casefold() == PUBLICATION_DIRECTORY for part in path.parts) or name.startswith(".ai-translator-") or (
        name.startswith(".") and name.endswith((".ai-translator-publish.json", ".ai-translator-publish.lock"))
    )


def _same_path(first: Path, second: Path) -> bool:
    return first.resolve() == second.resolve() or (
        first.exists() and second.exists() and first.samefile(second)
    )


def validate_job_paths(reads: list[Path], state: Path | None, outputs: list[Path], force: bool | set[Path]) -> None:
    """Check file identities without opening the cache or modifying recovery files."""
    if state is not None:
        if state.exists() and not state.is_file():
            raise ValueError(f"Для кеша нужен путь к файлу SQLite. {state}")
        if any(_same_path(state, path) for path in reads):
            raise ValueError("Кеш должен храниться отдельно от исходных файлов.")
        reads = [*reads, state]
    for path in reads:
        if is_publication_sidecar(path.resolve()):
            raise ValueError(f"Имя зарезервировано для файлов восстановления: {path}")
    for index, path in enumerate(outputs):
        if is_publication_sidecar(output_path(path)):
            raise ValueError(f"Имя зарезервировано для файлов восстановления: {path}")
        if path_too_long(path):
            raise ValueError(f"Слишком длинный путь к выходному файлу. {path}")
        if any(_same_path(path, other) for other in reads + outputs[:index]):
            raise ValueError(f"Пути исходника, кеша и выходных файлов должны различаться: {path}")
        if not path.parent.is_dir() or path.is_dir():
            raise ValueError(f"Нужен путь к файлу в существующей папке: {path}")
        replace = output_path(path) in force if isinstance(force, set) else force
        if not replace and (path.exists() or path.is_symlink()):
            raise ValueError(f"Файл уже существует: {path}. Для замены укажите --force.")


@contextmanager
def _locked(paths: list[Path]):
    with ExitStack() as stack:
        for path in sorted({output_path(path) for path in paths}):
            lock = _sidecar(path, "lock", create=True)
            stack.enter_context(exclusive_lock(lock, f"Другой запуск сохраняет файл: {path}"))
        yield


def _sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except PermissionError:
        # A write-only drop directory can permit publication without a readable directory handle.
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
            raise
    finally:
        os.close(fd)


def _sync_directories(paths: Iterable[Path]) -> None:
    for path in dict.fromkeys(paths):
        _sync_directory(path)


def _atomic_text(path: Path, content: str) -> None:
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=".ai-translator-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        _sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _identity(path: Path) -> list[int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return [info.st_dev, info.st_ino]


@contextmanager
def _open_regular(path: Path, *, writable: bool = False):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Ожидается обычный файл.")
    flags = os.O_RDWR if writable else os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "r+b" if writable else "rb") as stream:
        if not os.path.samestat(before, os.fstat(stream.fileno())):
            raise ValueError("Файл изменился во время чтения.")
        yield stream


def _sync_prepared_file(path: Path) -> None:
    with _open_regular(path, writable=True) as stream:
        os.fsync(stream.fileno())


def _snapshot(source: Path, destination: Path) -> None:
    if os.name == "posix":
        try:
            os.link(source, destination, follow_symlinks=False)
        except OSError:
            pass
        else:
            if not destination.is_symlink():
                with _open_regular(destination) as stream:
                    os.fsync(stream.fileno())
            return
    if source.is_symlink():
        shutil.copy2(source, destination, follow_symlinks=False)
    else:
        with _open_regular(source) as original, destination.open("xb") as copy:
            shutil.copyfileobj(original, copy)
            copy.flush()
            os.fsync(copy.fileno())
        shutil.copystat(source, destination, follow_symlinks=False)


def _read_metadata(path: Path) -> str:
    try:
        with _open_regular(path) as stream:
            payload = stream.read(_MAX_METADATA + 1)
        if len(payload) > _MAX_METADATA:
            raise ValueError("Файл восстановления слишком велик.")
        return payload.decode("utf-8")
    except (OSError, ValueError) as exc:
        raise ValueError(f"Не удалось прочитать файл восстановления публикации. {path}") from exc


def _backup_digest(path: Path) -> str:
    try:
        if path.is_symlink():
            target = os.fsencode(os.readlink(path))
            return "link:" + hashlib.sha256(target).hexdigest()
        with _open_regular(path) as stream:
            return "file:" + hashlib.file_digest(stream, "sha256").hexdigest()
    except (OSError, ValueError) as exc:
        raise ValueError(f"Не удалось проверить резервную копию публикации. {path}") from exc


def _transaction_artifacts(path: Path, transaction: str, index: int) -> tuple[Path, Path]:
    suffix = f"{transaction}-{index}"
    return path.with_name(f".ai-translator-stage-{suffix}"), path.with_name(f".ai-translator-original-{suffix}")


@dataclass(frozen=True)
class _RecoveryEntry:
    path: Path
    staged: Path
    backup: Path | None
    identity: list[int] | None
    backup_digest: str | None

    @property
    def restoring(self) -> Path:
        return self.staged.with_name(self.staged.name.replace("-stage-", "-restore-", 1))


def _metadata_path(value: object) -> Path:
    if not isinstance(value, str) or "\x00" in value or not Path(value).is_absolute():
        raise ValueError("Ожидается абсолютный путь.")
    return Path(value)


def _read_journal(journal: Path, transaction: str) -> tuple[bool, list[_RecoveryEntry]]:
    def unique_fields(pairs):
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError("Повторяющиеся поля.")
        return fields

    try:
        record = json.loads(_read_metadata(journal), object_pairs_hook=unique_fields)
        if not isinstance(record, dict):
            raise ValueError("Ожидается объект журнала.")
        version = record.get("version", 1)
        if type(version) is not int or version not in (1, 2):
            raise ValueError("Неизвестная версия журнала.")
        committed, values = record["committed"], record["entries"]
        if type(committed) is not bool or not isinstance(values, list) or len(values) < 2:
            raise ValueError("Некорректное состояние публикации.")
        entries = []
        fields = {"path", "staged", "backup", "identity"}
        if version == 2:
            fields.add("backup_digest")
        for index, value in enumerate(values):
            if not isinstance(value, dict) or value.keys() != fields:
                raise ValueError("Неполная запись о выходном файле.")
            path = _metadata_path(value["path"])
            staged, backup = _transaction_artifacts(path, transaction, index)
            if _metadata_path(value["staged"]) != staged:
                raise ValueError("Некорректный путь временного файла.")
            if value["backup"] is None:
                backup = None
            elif _metadata_path(value["backup"]) != backup:
                raise ValueError("Некорректный путь резервной копии.")
            identity = value["identity"]
            if identity is not None and (not isinstance(identity, list) or len(identity) != 2
                                         or any(type(number) is not int or number < 0 for number in identity)):
                raise ValueError("Некорректный идентификатор файла.")
            digest = value.get("backup_digest")
            if version == 2 and backup is not None and identity is not None:
                if not isinstance(digest, str) or _BACKUP_DIGEST.fullmatch(digest) is None:
                    raise ValueError("Некорректная контрольная сумма резервной копии.")
            elif digest is not None:
                raise ValueError("Неожиданная контрольная сумма резервной копии.")
            entries.append(_RecoveryEntry(path, staged, backup, identity, digest))
        if len({entry.path for entry in entries}) != len(entries):
            raise ValueError("Повторяющиеся выходные файлы.")
        if len({entry.identity is None for entry in entries}) != 1 or committed and entries[0].identity is None:
            raise ValueError("Неполная запись о подготовленных файлах.")
    except (ValueError, KeyError, TypeError, RecursionError) as exc:
        raise ValueError(f"Повреждён журнал восстановления публикации. {journal}") from exc
    return committed, entries


def _artifact_identity(path: Path, *, allow_symlink: bool = False) -> list[int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not (stat.S_ISREG(info.st_mode) or allow_symlink and stat.S_ISLNK(info.st_mode)):
        raise ValueError(f"Некорректный временный файл публикации. {path}")
    return [info.st_dev, info.st_ino]


def _recover(paths: list[Path]) -> None:
    paths = [output_path(path) for path in paths]
    markers = [marker for path in paths if _identity(marker := _marker(path)) is not None]
    if not markers:
        return
    journals = set()
    for marker in markers:
        try:
            journals.add(_metadata_path(_read_metadata(marker)))
        except ValueError as exc:
            raise ValueError(f"Повреждён указатель журнала публикации. {marker}") from exc
    if len(journals) != 1:
        raise ValueError("Обнаружено несколько незавершённых публикаций для выбранных файлов.")
    journal = journals.pop()
    match = _JOURNAL_NAME.fullmatch(journal.name)
    if journal.parent not in {path.parent for path in paths} or match is None:
        raise ValueError(f"Некорректный путь к журналу публикации. {markers[0]}")
    committed, entries = _read_journal(journal, match[1])
    if set(paths) != {entry.path for entry in entries}:
        raise ValueError(f"Есть незавершённая публикация. Повторите исходную команду со всеми выходными файлами. {journal}")
    if committed and any(_identity(entry.path) != entry.identity for entry in entries):
        raise ValueError(f"Выходные файлы не соответствуют завершённой публикации. {journal}")
    rollback = []
    for entry in reversed(entries):
        staged = _artifact_identity(entry.staged)
        _artifact_identity(entry.restoring, allow_symlink=True)
        if staged is not None and entry.identity is not None and staged != entry.identity:
            raise ValueError(f"Временный файл публикации изменился. {entry.staged}")
        backup = _artifact_identity(entry.backup, allow_symlink=True) if entry.backup is not None else None
        if not committed and entry.identity is not None and _identity(entry.path) == entry.identity:
            if entry.backup is not None:
                if backup is None:
                    raise ValueError(f"Для восстановления публикации требуется резервная копия. {entry.backup}")
                if entry.backup_digest is not None and _backup_digest(entry.backup) != entry.backup_digest:
                    raise ValueError(f"Повреждена резервная копия публикации. {entry.backup}")
            rollback.append(entry)
    # A previous journal rename may have been visible even though its directory sync failed.
    _sync_directory(journal.parent)
    for entry in rollback:
        if entry.backup is not None:
            restoring = entry.restoring
            restoring.unlink(missing_ok=True)
            _snapshot(entry.backup, restoring)
            restoring.replace(entry.path)
        else:
            entry.path.unlink()
    # A prior rollback may have restored a name before it could sync that directory.
    _sync_directories(entry.path.parent for entry in entries)

    for entry in entries:
        entry.staged.unlink(missing_ok=True)
        entry.restoring.unlink(missing_ok=True)
        if entry.backup is not None:
            entry.backup.unlink(missing_ok=True)
    _sync_directories(entry.path.parent for entry in entries)
    for marker in markers:
        marker.unlink(missing_ok=True)
    _sync_directories(marker.parent for marker in markers)
    journal.unlink(missing_ok=True)
    _sync_directory(journal.parent)


def recover_outputs(paths: list[Path]) -> None:
    # Avoid creating sidecars when there is nothing to recover.
    if any(_identity(_marker(output_path(path))) is not None for path in paths):
        with _locked(paths):
            _recover(paths)


def _publish_new(source: Path, destination: Path) -> None:
    if os.name == "nt":
        source.rename(destination)  # Windows rename fails if the destination exists.
    else:
        os.link(source, destination)


def check_publication(paths: list[Path], force: bool | set[Path]) -> None:
    for path in paths:
        target = output_path(path)
        if _sidecar(target, "lock").parent != target.parent:
            with _locked([target]):
                pass
    if force is True:
        return
    replacements = {output_path(path) for path in force} if isinstance(force, set) else set()
    directories = {target.parent for path in paths if (target := output_path(path)) not in replacements}
    for directory in directories:
        with NamedTemporaryFile(dir=directory, prefix=".ai-translator-", delete=False) as stream:
            source = Path(stream.name)
        probe = source.with_name(source.name + "-probe")
        try:
            _publish_new(source, probe)
        except OSError as exc:
            raise ValueError(f"Папка не поддерживает безопасное создание выходных файлов: {directory}. {exc}") from exc
        else:
            probe.unlink()
        finally:
            source.unlink(missing_ok=True)


def _replace_pair(paths: list[Path], writers: list[Callable[[Path], None]], replacements: set[Path]) -> None:
    transaction = uuid4().hex
    journal = paths[0].parent / f".ai-translator-transaction-{transaction}.json"
    entries = []
    for index, path in enumerate(paths):
        staged, backup = _transaction_artifacts(path, transaction, index)
        entries.append({"path": str(path), "staged": str(staged), "identity": None, "backup_digest": None,
                        "backup": str(backup) if path in replacements and _identity(path) is not None else None})
    record = {"version": 2, "entries": entries, "committed": False}
    try:
        _atomic_text(journal, json.dumps(record))
        for path in paths:
            _atomic_text(_marker(path, create=True), str(journal))
        for path, entry, writer in zip(paths, entries, writers):
            temporary = Path(entry["staged"])
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            writer(temporary)
            _sync_prepared_file(temporary)
            entry["identity"] = _identity(temporary)
            if entry["backup"]:
                backup = Path(entry["backup"])
                _snapshot(path, backup)
                entry["backup_digest"] = _backup_digest(backup)
        _sync_directories(marker.parent.parent for path in paths
                          if (marker := _marker(path)).parent != path.parent)
        _sync_directories(path.parent for path in paths)
        _atomic_text(journal, json.dumps(record))
        for entry, path in zip(entries, paths):
            temporary = Path(entry["staged"])
            if path in replacements:
                temporary.replace(path)
            else:
                _publish_new(temporary, path)
        _sync_directories(path.parent for path in paths)
        record["committed"] = True
        _atomic_text(journal, json.dumps(record))
    finally:
        if any(_identity(_marker(path)) is not None for path in paths):
            _recover(paths)
        else:
            journal.unlink(missing_ok=True)


def write_outputs(outputs: list[tuple[Path, Callable[[Path], None]]], force: bool | set[Path]) -> None:
    paths = [output_path(path) for path, _ in outputs]
    for path in paths:
        if is_publication_sidecar(path):
            raise ValueError(f"Имя зарезервировано для файлов восстановления: {path}")
    replacements = {output_path(path) for path in force} if isinstance(force, set) else set(paths) if force else set()
    with _locked(paths):
        _recover(paths)
        if len(paths) > 1:
            _replace_pair(paths, [writer for _, writer in outputs], replacements)
            return
        path = paths[0]
        writer = outputs[0][1]
        with NamedTemporaryFile(dir=path.parent, prefix=".ai-translator-", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            writer(temporary)
            _sync_prepared_file(temporary)
            if path in replacements:
                temporary.replace(path)
            else:
                _publish_new(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        _sync_directory(path.parent)
