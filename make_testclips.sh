#!/usr/bin/env bash
# Generate two short test clips with burnt-in timecodes + tones.
set -e
export MSYS_NO_PATHCONV=1   # keep Windows-style paths intact inside filters
OUT="C:/Users/Zcc09/SyncPlayer/testmedia"
mkdir -p "$OUT"

FF="ffmpeg -y -hide_banner -loglevel error"

# Movie: 12 s, testsrc2, 440 Hz sine, white "MOVIE" + clock top-left
$FF -f lavfi -i "testsrc2=size=1280x720:rate=30:duration=12" \
    -f lavfi -i "sine=frequency=440:sample_rate=48000:duration=12" \
    -vf "drawtext=fontfile='C\\:/Windows/Fonts/arial.ttf':text='MOVIE %{pts\\:hms}':fontsize=64:fontcolor=white:borderw=4:bordercolor=black:x=40:y=40" \
    -c:v libx264 -preset veryfast -crf 20 -c:a aac -shortest \
    "$OUT/movie.mp4"

# Reaction: 10 s, smptebars, 660 Hz sine, yellow "REACT" + clock top-right
$FF -f lavfi -i "smptebars=size=1280x720:rate=30:duration=10" \
    -f lavfi -i "sine=frequency=660:sample_rate=48000:duration=10" \
    -vf "drawtext=fontfile='C\\\\:/Windows/Fonts/arial.ttf':text='REACT %{pts\\\\:hms}':fontsize=64:fontcolor=yellow:borderw=4:bordercolor=black:x=w-text_w-40:y=40" \
    -c:v libx264 -preset veryfast -crf 20 -c:a aac -shortest \
    "$OUT/react.mp4"

# bars.mp4: pitch-black letterbox (1280x540 content in 1280x720, 90px bars)
$FF -f lavfi -i "color=0x000000:size=1280x720:rate=30:duration=6" \
    -f lavfi -i "color=0x808080:size=1280x540:rate=30:duration=6" \
    -filter_complex "[0][1]overlay=0:90" \
    -c:v libx264 -pix_fmt yuv420p "$OUT/bars.mp4"

# bars_gray.mp4: DARK-GRAY letterbox (luma ~32) - the case the old
# cropdetect threshold 0 could NOT detect (the "Auto does nothing" bug)
$FF -f lavfi -i "color=0x101010:size=1280x720:rate=30:duration=6" \
    -f lavfi -i "color=0x808080:size=1280x540:rate=30:duration=6" \
    -filter_complex "[0][1]overlay=0:90" \
    -c:v libx264 -pix_fmt yuv420p "$OUT/bars_gray.mp4"

echo "OK:"
ls -la "$OUT"