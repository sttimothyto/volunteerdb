"""The manual's search model, pinned and fetched.

``manual_search`` embeds the manual with ``minishlab/potion-base-8M`` (a
model2vec static model: numpy-only inference, MIT licensed). The model is not
a dependency the lockfile can pin, so this module is the pin: one revision and
the sha256 of each file it needs, and ``fetch`` downloads exactly those, once,
into a directory model2vec loads without ever touching the network.

Standard library only, on purpose. The Containerfile's model stage copies in
this one file and runs it -- the build context excludes ``scripts/`` and the
package is not installed there -- and ``manual_search.load_embedder`` imports
it to check a directory before model2vec gets to see it. ``make model`` runs
the same code into ``.models/`` for development.
"""

import hashlib
import shutil
import sys
import urllib.request
from pathlib import Path

REPO = "minishlab/potion-base-8M"
REVISION = "bf8b056651a2c21b8d2565580b8569da283cab23"
# name -> (sha256, size in bytes), as served at REVISION. Everything
# model2vec's layout needs and nothing else: the ONNX export, the vocabulary
# and the sentence-transformers wrappers in the repository are not loaded.
FILES: dict[str, tuple[str, int]] = {
    "config.json": (
        "2a6ac0e9aaa356a68a5688070db78fc3a464fefe85d2f06a1905ce3718687553",
        202,
    ),
    "tokenizer.json": (
        "e67e803f624fb4d67dea1c730d06e1067e1b14d830e2c2202569e3ef0f70bb50",
        683666,
    ),
    "model.safetensors": (
        "f65d0f325faadc1e121c319e2faa41170d3fa07d8c89abd48ca5358d9a223de2",
        30236760,
    ),
}
BASE_URL = f"https://huggingface.co/{REPO}/resolve/{REVISION}"
_CHUNK = 1 << 20
# A download that stalls for this long is dead: start it over, up to
# ATTEMPTS times. Seen at implementation: the CDN went quiet 6 MB into the
# 30 MB safetensors file, and without a retry the image build would have
# failed on the production host for a hiccup.
TIMEOUT_S = 60
ATTEMPTS = 3


class FetchError(RuntimeError):
    """A download that came back wrong: the wrong bytes, or none."""


def _digest(path: Path) -> tuple[str, int]:
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while block := f.read(_CHUNK):
            sha.update(block)
            size += len(block)
    return sha.hexdigest(), size


def verified(target: Path, name: str) -> bool:
    """True when `name` is present under `target` with the pinned bytes."""
    path = target / name
    return path.is_file() and _digest(path) == FILES[name]


def missing(target: Path) -> tuple[str, ...]:
    """The files not yet present and correct under `target`; empty means
    the directory is the pinned model and can be loaded."""
    return tuple(name for name in FILES if not verified(target, name))


def _download(name: str, target: Path) -> None:
    # Through .part and a rename, so an interrupted download never leaves a
    # truncated file that is then trusted; a failed hash check removes it.
    part = target / f"{name}.part"
    request = urllib.request.Request(
        f"{BASE_URL}/{name}", headers={"User-Agent": "volunteerdb-manual-model"}
    )
    for attempt in range(1, ATTEMPTS + 1):
        try:
            # urllib follows the 307 to the CDN itself
            with (
                urllib.request.urlopen(request, timeout=TIMEOUT_S) as response,
                part.open("wb") as f,
            ):
                shutil.copyfileobj(response, f, _CHUNK)
            break
        except OSError as exc:  # includes URLError and socket timeouts
            part.unlink(missing_ok=True)
            if attempt == ATTEMPTS:
                raise FetchError(f"{name}: {exc} (after {ATTEMPTS} attempts)") from exc
            print(f"manual model: {name}: {exc}; retrying", file=sys.stderr)
    digest, size = _digest(part)
    expected_digest, expected_size = FILES[name]
    if (digest, size) != (expected_digest, expected_size):
        part.unlink()
        raise FetchError(
            f"{name}: got sha256 {digest} ({size} bytes), "
            f"pinned {expected_digest} ({expected_size} bytes)"
        )
    part.replace(target / name)


def fetch(target: Path) -> int:
    """Make `target` the pinned model; returns how many files were fetched.
    Idempotent: a file already present with the right hash is left alone, so
    a second run downloads nothing and a corrupted one is replaced."""
    target.mkdir(parents=True, exist_ok=True)
    fetched = 0
    for name in missing(target):
        _download(name, target)
        fetched += 1
    return fetched


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} TARGET_DIR", file=sys.stderr)
        return 2
    target = Path(argv[1])
    try:
        fetched = fetch(target)
    except (FetchError, OSError) as exc:
        print(f"manual model: {exc}", file=sys.stderr)
        return 1
    state = f"fetched {fetched} file(s)" if fetched else "already present"
    print(f"manual model {REPO}@{REVISION[:12]} in {target}: {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
