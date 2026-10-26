"""任意の動画ファイルを MP4 に変換するスクリプト。

フォルダ内の全動画を変換して <フォルダ>/output に保存し、
変換に成功した元ファイルは <フォルダ>/input に移動します。

使い方:
    python to_mp4.py                     # このスクリプトがあるフォルダを処理
    python to_mp4.py D:\\videos           # 指定フォルダを処理
    python to_mp4.py D:\\videos -r        # サブフォルダも含めて処理
    python to_mp4.py --crf 16            # さらに高画質
    python to_mp4.py --lossless          # 完全無劣化

ffmpeg が必要です。PATH 上の ffmpeg、無ければ imageio-ffmpeg 同梱の ffmpeg を使います。
    pip install imageio-ffmpeg
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

VIDEO_EXTS = {
    ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg",
    ".3gp", ".3g2", ".ts", ".mts", ".m2ts", ".vob", ".ogv", ".asf", ".rm",
    ".rmvb", ".divx", ".f4v", ".mxf", ".mp4",
}

# 再エンコードせずに MP4 へそのまま格納できるコーデック
MP4_VIDEO_CODECS = {"h264", "hevc", "av1", "mpeg4"}
MP4_AUDIO_CODECS = {"aac", "mp3", "alac", "ac3", "eac3", "opus"}

# ffmpeg -i の出力例: "Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / ...), ..."
STREAM_RE = re.compile(r"Stream #0:(\d+)\S*: (Video|Audio): (\w+)(.*)")


def find_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        sys.exit(
            "エラー: ffmpeg が見つかりません。\n"
            "  pip install imageio-ffmpeg\n"
            "を実行するか、ffmpeg をインストールして PATH に追加してください。"
        )


def probe_streams(ffmpeg: str, src: Path) -> tuple[tuple[int, str] | None, list[tuple[int, str]]]:
    """(映像ストリーム, 音声ストリーム一覧) を (番号, コーデック名) で返す。

    ffmpeg が解読できないストリーム（iPhone の空間オーディオ APAC など）は除外する。
    """
    result = subprocess.run([ffmpeg, "-hide_banner", "-i", str(src)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    video, audio = None, []
    for m in STREAM_RE.finditer(result.stderr):
        index, kind, codec, rest = int(m[1]), m[2], m[3], m[4]
        if codec == "none":
            continue
        if kind == "Video" and video is None and "attached pic" not in rest:
            video = (index, codec)
        elif kind == "Audio":
            audio.append((index, codec))
    return video, audio


def build_command(ffmpeg: str, src: Path, dst: Path, args,
                  video: tuple[int, str] | None, audio: list[tuple[int, str]],
                  allow_copy: bool) -> list[str]:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-stats",
           "-y" if args.overwrite else "-n", "-i", str(src), "-map_metadata", "0"]

    if video:
        cmd += ["-map", f"0:{video[0]}"]
        if allow_copy and video[1] in MP4_VIDEO_CODECS:
            cmd += ["-c:v", "copy"]
            if video[1] == "hevc":
                cmd += ["-tag:v", "hvc1"]  # Apple 製品や QuickTime で再生できるようにする
        elif args.lossless:
            # 完全無劣化（色形式も元のまま）。ファイルは大きく、再生できない機器もある
            cmd += ["-c:v", "libx264", "-preset", args.preset, "-qp", "0"]
        else:
            cmd += [
                "-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
                "-pix_fmt", "yuv420p",
                # libx264 は奇数サイズを扱えない。拡縮すると画質が落ちるため 1px の余白を足して偶数にする
                "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
            ]

    for out_index, (index, codec) in enumerate(audio):
        cmd += ["-map", f"0:{index}"]
        if allow_copy and codec in MP4_AUDIO_CODECS:
            cmd += [f"-c:a:{out_index}", "copy"]
        else:
            cmd += [f"-c:a:{out_index}", "aac", f"-b:a:{out_index}", args.audio_bitrate]

    cmd += ["-movflags", "+faststart", str(dst)]
    return cmd


def convert(ffmpeg: str, src: Path, dst: Path, args) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)

    video, audio = probe_streams(ffmpeg, src)
    if not video and not audio:
        print(f"  失敗（読み取れるストリームがありません）: {src}", file=sys.stderr)
        return False

    # MP4 互換のストリームは再エンコードせずそのまま格納する（無劣化）
    allow_copy = not args.reencode
    copy_video = allow_copy and video is not None and video[1] in MP4_VIDEO_CODECS
    copy_audio = allow_copy and bool(audio) and all(c in MP4_AUDIO_CODECS for _, c in audio)
    mode = f"映像:{'コピー' if copy_video else '再エンコード'} 音声:{'コピー' if copy_audio else '再エンコード'}"
    print(f"変換中 [{mode}]: {src} -> {dst}")

    result = subprocess.run(build_command(ffmpeg, src, dst, args, video, audio, allow_copy))
    if result.returncode != 0 and allow_copy:
        print("  コピーに失敗したため再エンコードします...")
        dst.unlink(missing_ok=True)
        result = subprocess.run(build_command(ffmpeg, src, dst, args, video, audio, False))
    if result.returncode != 0:
        print(f"  失敗: {src}", file=sys.stderr)
        dst.unlink(missing_ok=True)
        return False
    print("  完了")
    return True


def collect_videos(folder: Path, recursive: bool, exclude: list[Path]) -> list[Path]:
    """フォルダ内の動画ファイルを返す（output / input フォルダ内は除外）。"""
    pattern = "**/*" if recursive else "*"
    return [
        f for f in sorted(folder.glob(pattern))
        if f.is_file() and f.suffix.lower() in VIDEO_EXTS
        and not any(f.is_relative_to(d) for d in exclude)
    ]


def unique_path(path: Path) -> Path:
    """同名ファイルがあれば name (1).ext のように連番を付ける。"""
    if not path.exists():
        return path
    i = 1
    while (candidate := path.with_name(f"{path.stem} ({i}){path.suffix}")).exists():
        i += 1
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser(
        description="フォルダ内の全動画を MP4 に変換して output に保存し、元ファイルを input に移動します。")
    parser.add_argument("folder", nargs="?", type=Path, default=Path(__file__).resolve().parent,
                        help="処理するフォルダ（省略時はこのスクリプトがあるフォルダ）")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="サブフォルダも含めて処理する")
    parser.add_argument("-y", "--overwrite", action="store_true",
                        help="output 内の同名ファイルを上書きする（既定は連番を付けて別名保存）")
    parser.add_argument("--reencode", action="store_true",
                        help="互換コーデックでも常に再エンコードする")
    parser.add_argument("--crf", type=int, default=18,
                        help="画質 (0-51, 小さいほど高画質, 既定: 18 = 見た目ではほぼ劣化なし)")
    parser.add_argument("--preset", default="slow",
                        help="x264 プリセット (ultrafast〜veryslow, 遅いほど同サイズで高画質, 既定: slow)")
    parser.add_argument("--audio-bitrate", default="320k",
                        help="音声を再エンコードする場合のビットレート (既定: 320k)")
    parser.add_argument("--lossless", action="store_true",
                        help="映像を完全無劣化で再エンコード（サイズ大・互換性低）")
    args = parser.parse_args()

    folder = args.folder.resolve()
    if not folder.is_dir():
        print(f"フォルダが見つかりません: {folder}", file=sys.stderr)
        return 1
    output_dir = folder / "output"
    input_dir = folder / "input"

    ffmpeg = find_ffmpeg()
    videos = collect_videos(folder, args.recursive, [output_dir, input_dir])
    if not videos:
        print(f"変換対象の動画が見つかりませんでした: {folder}", file=sys.stderr)
        return 1

    failed = 0
    for src in videos:
        rel = src.relative_to(folder)
        dst = output_dir / rel.with_suffix(".mp4")
        if not args.overwrite:
            # a.avi と a.mkv のように同名の動画があっても上書きしない
            dst = unique_path(dst)
        if not convert(ffmpeg, src, dst, args):
            failed += 1
            continue
        # 変換に成功した元ファイルだけ input へ移動（失敗したものは元の場所に残す）
        moved = unique_path(input_dir / rel)
        moved.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(src, moved)
        print(f"  元ファイルを移動: {moved}")

    print(f"\n完了: {len(videos) - failed} 件成功, {failed} 件失敗")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
