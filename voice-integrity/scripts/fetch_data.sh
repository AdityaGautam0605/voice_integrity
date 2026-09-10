#!/usr/bin/env bash
# Corpus download helper.
#
# ASVspoof requires accepting terms on Edinburgh DataShare before the link
# works.  Start that on day one - people will otherwise sit idle waiting.
set -euo pipefail

RAW_DIR="${1:-data/raw}"
mkdir -p "$RAW_DIR"

echo "=== preflight ==="
if command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg: present"
  if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q amr; then
    echo "AMR-NB encoder: present"
  else
    echo "AMR-NB encoder: MISSING"
    echo "  Stock builds often decode but cannot encode AMR-NB, in which case"
    echo "  your mobile-codec augmentation silently does nothing."
    echo "  Fix:  sudo apt install libopencore-amrnb-dev  (then rebuild ffmpeg)"
    echo "  Or:   drop amr_nb from configs/augment.yaml and say so in the report."
  fi
else
  echo "ffmpeg: MISSING - codec augmentation cannot run"
fi

echo
echo "=== manual downloads ==="
cat <<'NOTES'
1. ASVspoof 2019 LA          https://datashare.ed.ac.uk/handle/10283/3336
   Accept the terms first.  Unpack so that data/raw/LA/ contains
   ASVspoof2019_LA_train/, ASVspoof2019_LA_dev/, ASVspoof2019_LA_eval/
   and ASVspoof2019_LA_cm_protocols/.

2. In-the-Wild               https://deepfake-total.com/in_the_wild
   Unpack to data/raw/in_the_wild/ with its meta.csv alongside the audio.

3. AI4Bharat Kathbath        https://github.com/AI4Bharat/IndicSUPERB
   A per-language subset is enough - a few hundred clips builds the
   Indian-language evaluation set.

Then:
   PYTHONPATH=src python scripts/build_manifests.py \
       --asvspoof data/raw/LA \
       --in-the-wild data/raw/in_the_wild
NOTES

echo
echo "=== model cache ==="
echo "Point HF_HOME and TORCH_HOME at the repo BEFORE first use, or the"
echo "offline demo will try to reach the network at the venue:"
echo '  export HF_HOME="$PWD/cache/huggingface"'
echo '  export TORCH_HOME="$PWD/cache/torch"'
