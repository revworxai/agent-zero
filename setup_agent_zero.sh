#!/bin/zsh
# ============================================================
# Agent Zero - macOS Local Setup Script
# Tested on: Intel iMac, macOS, VSCodium, Miniconda
# Python: 3.12 (3.13 incompatible, 3.11 also works)
# Usage: Run from agent-zero project root directory
# ============================================================

set -e

CONDA_SH="/usr/local/Caskroom/miniconda/base/etc/profile.d/conda.sh"
ENV_NAME="agent-zero"
PYTHON_VERSION="3.12"
CONDA_ENV_BIN="/usr/local/Caskroom/miniconda/base/envs/$ENV_NAME/bin"
PYTHON_BIN="$CONDA_ENV_BIN/python3"
PIP_BIN="$CONDA_ENV_BIN/pip"

echo ""
echo "============================================================"
echo " Agent Zero - macOS Setup Script"
echo "============================================================"
echo ""

# --- Step 1: Source Conda ---
echo "[1/10] Sourcing Conda..."
if [ -f "$CONDA_SH" ]; then
    source "$CONDA_SH"
else
    echo "❌ Conda not found at $CONDA_SH"
    echo "   Install Miniconda: brew install --cask miniconda"
    exit 1
fi

# --- Step 2: Remove existing environment ---
echo "[2/10] Removing existing '$ENV_NAME' environment (if any)..."
conda deactivate 2>/dev/null || true
conda env remove -n "$ENV_NAME" --yes 2>/dev/null || echo "   (none found - continuing)"

# --- Step 3: Create fresh environment ---
echo "[3/10] Creating Conda environment with Python $PYTHON_VERSION..."
conda create -n "$ENV_NAME" python="$PYTHON_VERSION" --yes

# --- Step 4: Activate ---
echo "[4/10] Activating environment..."
conda activate "$ENV_NAME"

# --- Step 5: Native binary deps via conda-forge ---
echo "[5/10] Installing native binary dependencies via conda-forge..."
conda install -c conda-forge llvmlite pikepdf --yes

# --- Step 6: Core requirements ---
echo "[6/10] Installing core requirements (requirements.txt)..."
"$PIP_BIN" install -r requirements.txt

# --- Step 7: Version conflict overrides + litellm ---
echo "[7/10] Installing version-pinned overrides (requirements2.txt)..."
if [ -f requirements2.txt ]; then
    "$PIP_BIN" install -r requirements2.txt
else
    echo "   ⚠️  requirements2.txt not found - installing litellm directly"
    "$PIP_BIN" install litellm
fi

# --- Step 8: Playwright Chromium ---
echo "[8/10] Installing Playwright Chromium browser..."
"$PYTHON_BIN" -m playwright install chromium

# --- Step 9: Fix PATH in ~/.zshrc ---
echo "[9/10] Ensuring conda env takes PATH priority..."
if ! grep -q "envs/$ENV_NAME/bin" ~/.zshrc; then
    echo "export PATH=\"$CONDA_ENV_BIN:\$PATH\"" >> ~/.zshrc
    echo "   ✅ Added conda env to top of PATH in ~/.zshrc"
else
    echo "   ✅ PATH already configured."
fi

# --- Step 10: Configure VSCodium/VSCode interpreter ---
echo "[10/10] Configuring VSCodium Python interpreter in .vscode/settings.json..."
mkdir -p .vscode
"$PYTHON_BIN" - << PYEOF
import json
import os

settings_path = ".vscode/settings.json"
interpreter_path = "$PYTHON_BIN"

# Read existing settings if file exists
if os.path.exists(settings_path) and os.path.getsize(settings_path) > 0:
    with open(settings_path, "r") as f:
        try:
            settings = json.load(f)
        except json.JSONDecodeError:
            print("   ⚠️  Existing settings.json could not be parsed - creating backup")
            os.rename(settings_path, settings_path + ".bak")
            settings = {}
else:
    settings = {}

# Add/update the interpreter path
settings["python.defaultInterpreterPath"] = interpreter_path

# Write back with clean formatting
with open(settings_path, "w") as f:
    json.dump(settings, f, indent=4)
    f.write("\n")

print(f"   ✅ Set python.defaultInterpreterPath to: {interpreter_path}")
PYEOF

echo ""
echo "============================================================"
echo " ✅ Setup Complete!"
echo "============================================================"
echo ""
echo " Next steps:"
echo "   1. cp .env.example usr/.env"
echo "   2. nano usr/.env  (add your API keys)"
echo "   3. Open a new terminal"
echo "   4. conda activate $ENV_NAME"
echo "   5. python3 run_ui.py"
echo "   6. Open http://localhost:5757"
echo ""
echo " Note: 'No RFC password' errors in terminal are harmless."
echo "============================================================"
