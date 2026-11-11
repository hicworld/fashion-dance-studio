"""Collect notices for a local PyInstaller build, without implicit downloads."""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import re
import sys
import tempfile
from importlib import metadata
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "build" / "release-license-cache"
ANALYSIS = ROOT / "build" / "desktop" / "FashionDanceStudio"
MAX_NOTICE = 2 * 1024**2
NOTICE = re.compile(r"(?:license|licence|copying|copyright|notice)(?:[._-][a-z0-9_.-]+)?$", re.I)
NOTICE_SUFFIXES = {"", ".txt", ".md", ".rst", ".terms", ".bsd", ".mit", ".apache", ".gpl"}
TYPES = {"DATA", "BINARY", "EXTENSION", "PYSOURCE", "PYSOURCE-1", "PYMODULE", "PYMODULE-1"}
FAL_REV = "d82d13d28433e36f29cad1e79810826814538065"
PROXY_REV = "db43f1e35d4f90a65c5a4d56d9e9af88212ec6e6"
WEBVIEW_VERSION = "1.0.3856.49"
UPSTREAM = {
    "fal-client-0.14.1/LICENSE.txt": (
        f"https://raw.githubusercontent.com/fal-ai/fal/{FAL_REV}/LICENSE",
        "c5cd488c00dea64e15d64d4a5a7832cede329f2c31a481890cf8a272107c4c45",
    ),
    "proxy-tools-0.1.0/LICENSE.txt": (
        f"https://raw.githubusercontent.com/jtushman/proxy_tools/{PROXY_REV}/LICENSE.txt",
        "a428fb8a2e762af3eb0a6edbbb88e9b42ccfee80fd9b423958bcacf9b9abbfe4",
    ),
}
WEBVIEW_NUPKG = (
    f"https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/{WEBVIEW_VERSION}/"
    f"microsoft.web.webview2.{WEBVIEW_VERSION}.nupkg"
)
WEBVIEW_NOTICES = {
    "LICENSE.txt": "0af8f1b807512aae39c2ac1aa4d0cae65cabecb6fd554b8439a5162a0d6eca55",
    "NOTICE.txt": "106423785c5b7eba0a8e61d1837f2132e9c828e20ad530f565d981c1df60dd90",
}
NATIVE_SOURCES = {
    "bzip2": "https://sourceware.org/bzip2/",
    "libffi": "https://github.com/libffi/libffi",
    "openssl": "https://github.com/openssl/openssl",
    "sqlite": "https://sqlite.org/copyright.html",
    "tk": "https://www.tcl.tk/software/tcltk/",
    "xz": "https://tukaani.org/xz/",
    "zlib": "https://zlib.net/",
    "libzlib": "https://zlib.net/",
}
NATIVE_DLL = re.compile(r"(?:libbz2|libffi[-0-9]*|ffi|libcrypto[-.a-z0-9]*|libssl[-.a-z0-9]*|"
                        r"liblzma|libzlib|zlib|sqlite3|tcl\d+t?|tk\d+t?)\.dll$", re.I)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_text_bytes(path: Path) -> bytes:
    if path.stat().st_size > MAX_NOTICE:
        raise ValueError(f"Notice exceeds size limit: {path.name}")
    data = path.read_bytes()
    if not data.strip() or b"\0" in data:
        raise ValueError(f"Expected a nonempty text notice: {path.name}")
    return data


def fetch_verified(url: str, expected: str, limit: int) -> bytes:
    with urlopen(url, timeout=35) as response:
        if not response.geturl().startswith("https://"):
            raise ValueError("A notice source redirected away from HTTPS")
        data = response.read(limit + 1)
    if len(data) > limit or digest(data) != expected:
        raise ValueError(f"Unexpected source bytes: {url}")
    return data


def cached(relative: str, expected: str) -> bytes:
    path = CACHE / relative
    if path.is_file():
        data = read_text_bytes(path)
        if digest(data) == expected:
            return data
    raise ValueError(f"Missing or invalid {relative}; run collect_release_notices.py --prepare-upstream")


def put_cache(relative: str, data: bytes) -> None:
    path = CACHE / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        tmp = Path(file.name)
        file.write(data)
    try:
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def prepare_upstream() -> None:
    for relative, (url, expected) in UPSTREAM.items():
        try:
            cached(relative, expected)
        except ValueError:
            put_cache(relative, fetch_verified(url, expected, MAX_NOTICE))
    try:
        for name, expected in WEBVIEW_NOTICES.items():
            cached(f"webview2-{WEBVIEW_VERSION}/{name}", expected)
    except ValueError:
        package = fetch_verified(
            WEBVIEW_NUPKG, "bc0f76eb911b569838dc4aa8f8d325269b966bedb592863d26211aef3a099f1a", 20 * 1024**2
        )
        with ZipFile(io.BytesIO(package)) as archive:
            for name, expected in WEBVIEW_NOTICES.items():
                data = archive.read(name)
                if len(data) > MAX_NOTICE or digest(data) != expected:
                    raise ValueError(f"Unexpected WebView2 notice: {name}")
                put_cache(f"webview2-{WEBVIEW_VERSION}/{name}", data)


def records(node):
    if isinstance(node, (list, tuple)):
        if len(node) == 3 and all(isinstance(item, str) for item in node) and node[2] in TYPES:
            yield tuple(node)
        else:
            for item in node:
                yield from records(item)


def source_records(release: Path, analysis: Path) -> list[tuple[str, Path, str]]:
    result = []
    for name in ("PYZ-00.toc", "Analysis-00.toc", "COLLECT-00.toc"):
        tree = ast.literal_eval((analysis / name).read_text(encoding="utf-8"))
        for dest, src, kind in records(tree):
            if name == "Analysis-00.toc" and not kind.startswith("PYSOURCE"):
                continue
            if name == "COLLECT-00.toc" and not (
                (release / "_internal" / dest).is_file() or (release / dest).is_file()
            ):
                continue
            result.append((dest, Path(src).resolve(), kind))
    return result


def normalized(value: str) -> str:
    value = re.sub(r"[-_.]+", "-", value).lower()
    if not re.fullmatch(r"[a-z0-9-]+", value):
        raise ValueError("Unexpected installed distribution name")
    return value


def is_notice(path: Path) -> bool:
    return bool(NOTICE.fullmatch(path.name) and path.suffix.lower() in NOTICE_SUFFIXES)


class Catalog:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.rows: list[tuple[str, str, str, list[str]]] = []

    def add(self, name: str, version: str, source: str, notices: list[tuple[str, bytes]]) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9.+_!-]+", version):
            raise ValueError(f"Unexpected version string for {name}")
        prefix = normalized(name) + "-" + version
        paths = []
        fingerprints = set()
        for filename, data in notices:
            fingerprint = digest(data)
            if fingerprint in fingerprints:
                continue
            fingerprints.add(fingerprint)
            safe_name = re.sub(r"[^a-zA-Z0-9._-]", "-", filename)
            path = f"{prefix}/{safe_name}"
            if path in self.files:
                path = f"{prefix}/{fingerprint[:12]}-{safe_name}"
            self.files[path] = data
            paths.append(path)
        self.rows.append((name, version, source, paths))

    def index(self) -> bytes:
        lines = [
            "# Third-party notices",
            "",
            "The components below are used by this Windows build. Each component retains its own terms.",
            "Software obtained separately at first launch is documented in the application guide.",
            "",
            "| Component | Version | Upstream | Local notice files |",
            "| --- | --- | --- | --- |",
        ]
        for name, version, source, paths in sorted(self.rows, key=lambda row: row[0].casefold()):
            links = " · ".join(f"[{Path(path).name}]({path})" for path in paths) or "See upstream"
            lines.append(f"| {name} | {version} | [Source]({source}) | {links} |")
        return ("\n".join(lines) + "\n").encode("utf-8")


def wheel_notices(catalog: Catalog, used: set[Path]) -> set[str]:
    included = set()
    for dist in sorted(metadata.distributions(), key=lambda d: (d.metadata["Name"] or "").casefold()):
        files = tuple(dist.files or ())
        if not any(Path(dist.locate_file(file)).resolve() in used for file in files):
            continue
        name = normalized(dist.metadata["Name"])
        version = dist.version
        if name in included:
            continue
        included.add(name)
        notices = [
            (file.name, read_text_bytes(Path(dist.locate_file(file))))
            for file in files if is_notice(Path(file.name)) and Path(dist.locate_file(file)).is_file()
        ]
        if not notices:
            relative = f"{name}-{version}/LICENSE.txt"
            if relative not in UPSTREAM:
                raise ValueError(f"No packaged license found for {name} {version}; review its upstream notices")
            notices.append(("LICENSE.txt", cached(relative, UPSTREAM[relative][1])))
        origin = {
            "fal-client": f"https://github.com/fal-ai/fal/tree/{FAL_REV}/projects/fal_client",
            "proxy-tools": f"https://github.com/jtushman/proxy_tools/tree/{PROXY_REV}",
            "pyinstaller": "https://pyinstaller.org/en/stable/license.html",
        }.get(name, f"https://pypi.org/project/{name}/{version}/")
        catalog.add(name, version, origin, notices)
    return included


def conda_notices(catalog: Catalog, used: set[Path], base: Path) -> None:
    relative_files = {path.relative_to(base).as_posix().lower() for path in used if path.is_relative_to(base)}
    for name, origin in NATIVE_SOURCES.items():
        for info in sorted((base / "conda-meta").glob(f"{name}-*.json")):
            meta = json.loads(info.read_text(encoding="utf-8"))
            if meta.get("name") != name or not relative_files.intersection(
                entry.lower() for entry in meta.get("files", ()) if isinstance(entry, str)
            ):
                continue
            # Deliberately derive the cache path locally, without using arbitrary
            # paths or channel URLs from the environment's metadata.
            package = base / "pkgs" / info.stem
            paths = sorted(p for p in (package / "info" / "licenses").rglob("*") if p.is_file() and is_notice(p))
            paths += [p for p in (package / "info" / "LICENSE.txt",) if p.is_file()]
            if name == "tk":
                tk_terms = base / "Library" / "lib" / "tk8.6" / "license.terms"
                if tk_terms.is_file():
                    paths.append(tk_terms)
            notices = [(p.name, read_text_bytes(p)) for p in paths]
            if name in {"zlib", "libzlib"}:
                header = package / "info" / "licenses" / "zlib.h"
                if header.is_file():
                    # Some Conda zlib packages ship the license in a C header.
                    # Preserve the leading notice only, not header source.
                    first_comment = re.match(rb"/\*(.*?)\*/", read_text_bytes(header), re.S)
                    if first_comment and b"Copyright" in first_comment[1]:
                        notices.append(("LICENSE.txt", first_comment[1].strip() + b"\n"))
            if not notices and name != "sqlite":
                raise ValueError(f"Missing Conda package notices for {name}; restore the local package cache")
            catalog.add("Tcl-Tk" if name == "tk" else name, meta["version"], origin, notices)


def gather(release: Path, analysis: Path) -> Catalog:
    selected = source_records(release, analysis)
    used = {source for _, source, _ in selected}
    base = Path(sys.base_prefix).resolve()
    if (base / "conda-meta").is_dir():
        foreign = sorted({path.name for path in used if NATIVE_DLL.fullmatch(path.name)
                          and not path.is_relative_to(base)})
        if foreign:
            raise ValueError("Native libraries resolved outside the build interpreter: "
                             + ", ".join(foreign) + "; clean PATH and rebuild")
    catalog = Catalog()
    included = wheel_notices(catalog, used)
    if "pyinstaller" not in included:
        raise ValueError("Cannot identify the build interpreter's PyInstaller runtime")
    python_license = next((path for path in (base / "LICENSE_PYTHON.txt", base / "LICENSE.txt", base / "LICENSE")
                           if path.is_file()), None)
    if python_license is None:
        raise ValueError("Missing CPython license in the build interpreter's base installation")
    python_version = ".".join(str(n) for n in sys.version_info[:3])
    catalog.add("CPython", python_version, f"https://github.com/python/cpython/tree/v{python_version}",
                [(python_license.name, read_text_bytes(python_license))])
    conda_notices(catalog, used, base)
    sdk_names = {"microsoft.web.webview2.core.dll", "microsoft.web.webview2.winforms.dll", "webview2loader.dll"}
    if any(source.name.lower() in sdk_names for source in used):
        if metadata.version("pywebview") != "6.2.1":
            raise ValueError("Review the bundled WebView2 SDK version before updating pywebview notices")
        catalog.add("Microsoft-WebView2-SDK", WEBVIEW_VERSION,
                    f"https://www.nuget.org/packages/Microsoft.Web.WebView2/{WEBVIEW_VERSION}",
                    [(name, cached(f"webview2-{WEBVIEW_VERSION}/{name}", expected))
                     for name, expected in WEBVIEW_NOTICES.items()])
    return catalog


def write_notices(release: Path, catalog: Catalog) -> None:
    target = release / "licenses"
    if target.exists():
        raise ValueError("The release already has a licenses directory; use a fresh build")
    with tempfile.TemporaryDirectory(dir=release, prefix=".notices-") as directory:
        stage = Path(directory) / "licenses"
        stage.mkdir()
        for relative, data in catalog.files.items():
            path = stage / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (stage / "README.md").write_bytes(catalog.index())
        stage.rename(target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", type=Path, nargs="?")
    parser.add_argument("--analysis-dir", type=Path, default=ANALYSIS)
    parser.add_argument("--prepare-upstream", action="store_true",
                        help="Explicitly fetch the pinned notices missing from upstream wheels")
    args = parser.parse_args()
    if args.prepare_upstream:
        if args.release is not None:
            parser.error("prepare notices separately before building a release")
        prepare_upstream()
        print("Pinned upstream notices verified.")
    else:
        if args.release is None:
            parser.error("provide the PyInstaller release directory")
        release = args.release.resolve(strict=True)
        if not (release / "_internal").is_dir():
            parser.error("expected a PyInstaller onedir release")
        catalog = gather(release, args.analysis_dir)
        write_notices(release, catalog)
        print(f"Third-party notices: {len(catalog.rows)} components, {len(catalog.files)} documents.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
