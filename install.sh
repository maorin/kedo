#!/bin/sh
# kedo install script — compatible with macOS and Ubuntu 24.04+
# Usage: sh install.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { printf "${GREEN}[kedo]${NC} %s\n" "$1"; }
warn()  { printf "${YELLOW}[kedo]${NC} %s\n" "$1"; }
error() { printf "${RED}[kedo]${NC} %s\n" "$1"; }

# -----------------------------------------------------------
# 1. Check Python version
# -----------------------------------------------------------
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" >/dev/null 2>&1; then
        ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] 2>/dev/null && [ "$minor" -ge 10 ] 2>/dev/null; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    error "Python >= 3.10 required. Install it first:"
    echo "  Ubuntu: sudo apt install python3 python3-venv python3-pip"
    echo "  macOS:  brew install python@3.12"
    exit 1
fi

info "Using $PYTHON ($($PYTHON --version))"

# -----------------------------------------------------------
# 2. Ensure python3-venv is available (Ubuntu/Debian)
# -----------------------------------------------------------
if [ "$(uname)" = "Linux" ]; then
    if ! "$PYTHON" -c "import ensurepip" >/dev/null 2>&1; then
        warn "python3-venv not installed, installing..."
        sudo apt update && sudo apt install -y python3-venv python3-pip
    fi
fi

# -----------------------------------------------------------
# 3. Create virtual environment
# -----------------------------------------------------------
if [ ! -d "$VENV_DIR" ]; then
    info "Creating virtual environment at $VENV_DIR ..."
    "$PYTHON" -m venv "$VENV_DIR"
else
    info "Virtual environment already exists at $VENV_DIR"
fi

# Activate (POSIX compatible — use . instead of source)
. "$VENV_DIR/bin/activate"
info "Activated venv: $(which python)"

# -----------------------------------------------------------
# 4. Install kedo
# -----------------------------------------------------------
info "Installing kedo and dependencies..."
pip install --upgrade pip -q
pip install -e "$SCRIPT_DIR" -q

# Verify
if [ -x "$VENV_DIR/bin/kedo" ]; then
    info "kedo installed: $VENV_DIR/bin/kedo"
else
    error "kedo command not found in venv"
    exit 1
fi

# -----------------------------------------------------------
# 5. Create global wrapper script
# -----------------------------------------------------------
VENV_KEDO="$VENV_DIR/bin/kedo"

if [ "$(uname)" = "Darwin" ]; then
    BIN_DIR="/usr/local/bin"
else
    BIN_DIR="$HOME/.local/bin"
    mkdir -p "$BIN_DIR"
fi

WRAPPER="$BIN_DIR/kedo"

# Write wrapper to temp file first
TMP_WRAPPER=$(mktemp)
cat > "$TMP_WRAPPER" <<WRAPPER_EOF
#!/bin/sh
export VIRTUAL_ENV="$VENV_DIR"
export PATH="$VENV_DIR/bin:\$PATH"
exec "$VENV_KEDO" "\$@"
WRAPPER_EOF
chmod +x "$TMP_WRAPPER"

if [ "$(uname)" = "Darwin" ]; then
    sudo mv "$TMP_WRAPPER" "$WRAPPER"
else
    mv "$TMP_WRAPPER" "$WRAPPER"
fi

info "Wrapper installed: $WRAPPER"

# -----------------------------------------------------------
# 6. Ensure ~/.local/bin is on PATH (Linux)
# -----------------------------------------------------------
if [ "$(uname)" = "Linux" ]; then
    NEED_PATH_UPDATE=false
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *) NEED_PATH_UPDATE=true ;;
    esac

    if [ "$NEED_PATH_UPDATE" = true ]; then
        SHELL_NAME="$(basename "$SHELL")"
        RC_FILE=""
        case "$SHELL_NAME" in
            zsh)  RC_FILE="$HOME/.zshrc" ;;
            bash) RC_FILE="$HOME/.bashrc" ;;
        esac

        if [ -n "$RC_FILE" ]; then
            if ! grep -q '\.local/bin' "$RC_FILE" 2>/dev/null; then
                printf '\n# kedo: add ~/.local/bin to PATH\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$RC_FILE"
                warn "Added ~/.local/bin to PATH in $RC_FILE"
            fi
        fi
    fi
fi

# -----------------------------------------------------------
# Done
# -----------------------------------------------------------
echo ""
info "Installation complete!"
echo ""
echo "  Usage:"
echo "    kedo                       # Start interactive REPL"
echo "    kedo /path/to/project      # Specify project path"
echo "    kedo server                # Web server only"
echo ""

if [ "$(uname)" = "Linux" ]; then
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *) warn "Run '. ~/.$(basename "$SHELL")rc' or open a new terminal first" ;;
    esac
fi
