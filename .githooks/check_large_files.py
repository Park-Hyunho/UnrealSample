"""pre-commit: 큰 파일 · LFS 누락 바이너리를 커밋 전에 막는다.

막는 경우 (기본값, git config 로 조정 가능)
  1. LFS 가 아닌 파일이 10MB 이상              hooks.sizecheck.nonlfsmaxmb   (GitHub 은 100MB 에서 push 거부)
  2. LFS 가 아닌 바이너리가 512KB 이상           hooks.sizecheck.binarymaxkb   (.gitattributes 패턴 누락 의심)
  3. LFS 파일 하나가 100MB 이상                  hooks.sizecheck.lfsmaxmb      (LFS 무료 용량 소모 · 원본 8K 텍스처 등)
  4. 이번 커밋에 새로 들어가는 LFS 합계 500MB 이상  hooks.sizecheck.lfstotalmaxmb (에셋 팩 통째 커밋)

한 번만 통과시키기 (GitHub Desktop 에서도 동작):
  git config hooks.sizecheck.allowonce true   → 다음 커밋 1회 검사 생략 후 자동 해제
CLI 에서는 git commit --no-verify 도 가능.
"""
import subprocess
import sys

DEFAULTS = {"nonlfsmaxmb": 10, "binarymaxkb": 512, "lfsmaxmb": 100, "lfstotalmaxmb": 500}
MB = 1024 * 1024


def git(*args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(["git", *args], input=data, capture_output=True, check=True).stdout


def config_value(key: str) -> str:
    try:
        return git("config", "--get", f"hooks.sizecheck.{key}").decode().strip()
    except subprocess.CalledProcessError:
        return ""


def limit(key: str) -> int:
    value = config_value(key)
    return int(value) if value.isdigit() else DEFAULTS[key]


def staged_files() -> list[str]:
    out = git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR")
    return [p for p in out.decode("utf-8").split("\0") if p]


def lfs_paths(paths: list[str]) -> set[str]:
    # check-attr -z 출력: path \0 attr \0 value \0 반복
    result: set[str] = set()
    chunk = 200  # 명령줄 길이 한계 회피
    for i in range(0, len(paths), chunk):
        out = git("check-attr", "-z", "filter", "--", *paths[i:i + chunk]).decode("utf-8").split("\0")
        for j in range(0, len(out) - 2, 3):
            if out[j + 2] == "lfs":
                result.add(out[j])
    return result


def read_blobs(paths: list[str], header_only: bool) -> dict[str, tuple[int, bytes]]:
    """스테이징된 blob 의 (크기, 앞부분) — cat-file --batch 한 번으로."""
    if not paths:
        return {}
    mode = "--batch-check" if header_only else "--batch"
    proc = subprocess.run(["git", "cat-file", mode], input="".join(f":{p}\n" for p in paths).encode("utf-8"),
                          capture_output=True, check=True)
    out, pos, result = proc.stdout, 0, {}
    for p in paths:
        end = out.index(b"\n", pos)
        header = out[pos:end].split()
        pos = end + 1
        size = int(header[2]) if len(header) >= 3 and header[1] != b"missing" else 0
        body = b""
        if not header_only and size:
            body = out[pos:pos + size]
            pos += size + 1
        result[p] = (size, body)
    return result


def lfs_pointer_size(pointer: bytes) -> int:
    for line in pointer.decode("utf-8", "replace").splitlines():
        if line.startswith("size "):
            return int(line[5:])
    return 0


def human(n: int) -> str:
    return f"{n / MB:,.1f}MB" if n >= MB else f"{n / 1024:,.0f}KB"


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    if config_value("allowonce") == "true":
        git("config", "--unset", "hooks.sizecheck.allowonce")
        print("[size-check] allowonce → 이번 커밋은 검사 생략 (설정 해제함)", file=sys.stderr)
        return 0

    files = staged_files()
    if not files:
        return 0
    lfs = lfs_paths(files)
    plain = [p for p in files if p not in lfs]

    nonlfs_max, bin_max = limit("nonlfsmaxmb") * MB, limit("binarymaxkb") * 1024
    lfs_max, lfs_total_max = limit("lfsmaxmb") * MB, limit("lfstotalmaxmb") * MB
    problems: list[str] = []

    sizes = read_blobs(plain, header_only=True)
    suspect = [p for p in plain if sizes[p][0] >= bin_max]  # 바이너리 판정은 큰 것만 내용 확인
    for p in plain:
        size = sizes[p][0]
        if size >= nonlfs_max:
            problems.append(f"LFS 아님 · {human(size)} ≥ {nonlfs_max // MB}MB : {p}")
    bodies = read_blobs([p for p in suspect if sizes[p][0] < nonlfs_max], header_only=False)
    for p, (size, body) in bodies.items():
        if b"\0" in body[:8000]:
            problems.append(f"LFS 아닌 바이너리 · {human(size)} : {p}  → .gitattributes 에 확장자 추가")

    pointers = read_blobs(sorted(lfs), header_only=False)
    total = 0
    for p, (_, body) in pointers.items():
        real = lfs_pointer_size(body)
        total += real
        if real >= lfs_max:
            problems.append(f"LFS 파일 · {human(real)} ≥ {lfs_max // MB}MB : {p}")
    if total >= lfs_total_max:
        problems.append(f"이번 커밋 LFS 합계 {human(total)} ≥ {lfs_total_max // MB}MB ({len(lfs)}개 파일)")

    if not problems:
        return 0
    print("\n[size-check] 커밋을 막았습니다 — 큰 파일이 있습니다.", file=sys.stderr)
    for line in problems:
        print("  - " + line, file=sys.stderr)
    print("\n  줄이기: 안 쓰는 에셋 빼기 · 텍스처 Maximum Texture Size 낮추기 · 에셋 팩은 Migrate 로 필요한 것만"
          "\n  그래도 올려야 하면: git config hooks.sizecheck.allowonce true  (다음 커밋 1회 통과)\n", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
