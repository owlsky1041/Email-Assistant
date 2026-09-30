#!/usr/bin/env bash
# 构建 macOS 发行包（必须在 macOS 上执行，且需要 Xcode 命令行工具）
#
#   ./packaging/build_macos.sh
#
# 产物：
#   dist/EmailAssistant.app                        应用包（双击即用，显示名“邮件管理助手”）
#   dist/email-assistant/                          命令行版本
#   dist/email-assistant-<版本>-macos-<架构>.tar.gz
#   dist/email-assistant-<版本>-macos-<架构>.dmg   （若安装了 create-dmg）
#
# ⚠️ 关于签名与公证
#   Apple Silicon 上未签名的可执行文件会被 Gatekeeper 拦截。分发给他人时
#   需要 Developer ID 证书签名 + 公证（notarytool），否则用户必须
#   右键 → 打开 才能绕过。本脚本在检测到证书时会自动签名，否则给出提示。
#
#   export CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
#   export APPLE_ID="you@example.com"
#   export APPLE_TEAM_ID="TEAMID"
#   export APPLE_APP_PASSWORD="app-specific-password"

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "!! 本脚本只能在 macOS 上运行（PyInstaller 不支持交叉编译）。"
  echo "   要得到 macOS 产物，请在 Mac 上执行，或使用 CI："
  echo "   .github/workflows/build.yml 中的 macos-latest 任务"
  exit 1
fi

VERSION="$(python3 -c "import re,pathlib;print(re.search(r'__version__ = \"([^\"]+)\"', pathlib.Path('src/__init__.py').read_text(encoding='utf-8')).group(1))" 2>/dev/null || echo "0.0.0")"
ARCH="$(uname -m)"   # arm64 或 x86_64
PY="${PYTHON:-python3}"
APP_NAME="邮件管理助手"
APP_BUNDLE_NAME="EmailAssistant.app"  # 目录名用 ASCII，显示名见 Info.plist

echo "==> 构建 macOS 发行包 v${VERSION} (${ARCH})"

# ---- 1. 依赖检查 ----------------------------------------------------------
if ! "$PY" -c "import PyInstaller" 2>/dev/null; then
  echo "!! 未安装 PyInstaller。请执行：pip install pyinstaller"
  exit 1
fi

for pkg in onnxruntime tokenizers chromadb pystray PIL; do
  if "$PY" -c "import $pkg" 2>/dev/null; then
    echo "    ✓ $pkg"
  else
    echo "    ! $pkg 未安装 —— 打包产物将缺少对应能力"
  fi
done

# ---- 2. 清理并构建 --------------------------------------------------------
rm -rf build dist
"$PY" -m PyInstaller packaging/email-assistant.spec --noconfirm --log-level WARN

APP_BUNDLE="dist/${APP_BUNDLE_NAME}"
if [ ! -d "$APP_BUNDLE" ]; then
  echo "!! 未生成 .app 应用包，请检查 spec 中的 BUNDLE 配置"
  exit 1
fi

echo "==> 冒烟测试"
./dist/email-assistant/email-assistant --version

# ---- 3. 代码签名（可选但强烈建议）----------------------------------------
if [ -n "${CODESIGN_IDENTITY:-}" ]; then
  echo "==> 使用 ${CODESIGN_IDENTITY} 签名"
  # --deep 已不推荐，改为对内部二进制逐个签名后再签外层
  find "$APP_BUNDLE/Contents" -type f \( -name "*.so" -o -name "*.dylib" \) -print0 \
    | xargs -0 -I{} codesign --force --timestamp --options runtime \
        --sign "$CODESIGN_IDENTITY" {} 2>/dev/null || true
  codesign --force --timestamp --options runtime \
    --sign "$CODESIGN_IDENTITY" "$APP_BUNDLE"
  codesign --verify --verbose=2 "$APP_BUNDLE"
  echo "    ✓ 签名完成"
else
  echo "!! 未设置 CODESIGN_IDENTITY，跳过签名。"
  echo "   未签名的应用在 Apple Silicon 上会被 Gatekeeper 拦截，"
  echo "   用户需「右键 → 打开」才能运行。正式分发请配置证书。"
fi

# ---- 4. 生成 DMG ----------------------------------------------------------
if command -v create-dmg >/dev/null 2>&1; then
  echo "==> 构建 DMG"
  DMG="dist/email-assistant-${VERSION}-macos-${ARCH}.dmg"
  rm -f "$DMG"
  create-dmg \
    --volname "$APP_NAME" \
    --window-size 600 400 \
    --icon-size 100 \
    --app-drop-link 450 180 \
    "$DMG" "$APP_BUNDLE" >/dev/null 2>&1 \
    && echo "    → $DMG" || echo "    ! DMG 构建失败（不影响 .app）"
else
  echo "    (跳过 DMG：未安装 create-dmg，可 brew install create-dmg)"
fi

# ---- 4.5 清理包内运行时残留 ----------------------------------------------
# 发行包绝不能带构建机的配置 / 数据库 / 日志
echo "==> 清理包内运行时残留"
rm -rf dist/email-assistant/config dist/email-assistant/data dist/email-assistant/logs
rm -rf "$APP_BUNDLE/Contents/MacOS/config" "$APP_BUNDLE/Contents/MacOS/data" \
       "$APP_BUNDLE/Contents/MacOS/logs"
find dist/email-assistant -name "*.tmp" -delete 2>/dev/null || true

# ---- 5. 命令行版 tar.gz ---------------------------------------------------
TARBALL="dist/email-assistant-${VERSION}-macos-${ARCH}.tar.gz"
echo "==> 打包 ${TARBALL}"
cat > dist/email-assistant/快速上手.txt <<'EOF'
腾讯企业邮箱邮件管理助手 —— macOS 命令行版
==========================================

图形界面版请直接双击 dist/EmailAssistant.app（访达中显示为“邮件管理助手”）

命令行用法：
  ./email-assistant init
  ./email-assistant auth set
  ./email-assistant doctor --check-imap
  ./email-assistant sync
  ./email-assistant search "报销发票"
  ./email-assistant serve

.app 版本的用户数据在：
  ~/Library/Application Support/EmailAssistant/
命令行版的数据在：本目录下 config/ data/ logs/
EOF
tar -czf "$TARBALL" -C dist email-assistant
echo "    → $TARBALL ($(du -h "$TARBALL" | cut -f1))"

# ---- 6. 公证（可选）-------------------------------------------------------
if [ -n "${APPLE_ID:-}" ] && [ -n "${APPLE_TEAM_ID:-}" ] && [ -n "${APPLE_APP_PASSWORD:-}" ]; then
  echo "==> 提交公证（notarytool）"
  ZIP="dist/notarize-${VERSION}.zip"
  ditto -c -k --keepParent "$APP_BUNDLE" "$ZIP"
  xcrun notarytool submit "$ZIP" \
    --apple-id "$APPLE_ID" \
    --team-id "$APPLE_TEAM_ID" \
    --password "$APPLE_APP_PASSWORD" \
    --wait
  xcrun stapler staple "$APP_BUNDLE"
  rm -f "$ZIP"
  echo "    ✓ 公证完成并已装订"
else
  echo "    (跳过公证：未设置 APPLE_ID / APPLE_TEAM_ID / APPLE_APP_PASSWORD)"
fi

echo
echo "==> 完成。产物在 dist/ 下。"
