#!/usr/bin/env bash
# 构建 Linux 发行包（原生构建，必须在 Linux 上执行）
#
#   ./packaging/build_linux.sh
#
# 产物：
#   dist/email-assistant/                    可执行目录（绿色版，解压即用）
#   dist/email-assistant-<版本>-linux-x86_64.tar.gz
#   dist/EmailAssistant-<版本>-linux-x86_64.AppImage（若安装了 appimagetool）
#
# 说明：PyInstaller 不支持交叉编译。要得到 Windows/macOS 产物，
#       必须在对应系统上运行各自的构建脚本，或使用 CI（见 .github/workflows/build.yml）。

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

VERSION="$(python -c "import re,pathlib;print(re.search(r'__version__ = \"([^\"]+)\"', pathlib.Path('src/__init__.py').read_text(encoding='utf-8')).group(1))" 2>/dev/null || echo "0.0.0")"
ARCH="$(uname -m)"
PY="${PYTHON:-python3}"

echo "==> 构建 Linux 发行包 v${VERSION} (${ARCH})"

# ---- 1. 依赖检查 ----------------------------------------------------------
if ! "$PY" -c "import PyInstaller" 2>/dev/null; then
  echo "!! 未安装 PyInstaller。请执行：pip install pyinstaller"
  exit 1
fi

if ! "$PY" -c "import sysconfig,os;print(sysconfig.get_config_var('LDLIBRARY'))" | grep -q '\.so'; then
  echo "!! 当前 Python 是静态构建，PyInstaller 需要共享库 libpython。"
  echo "   Debian/Ubuntu: sudo apt install libpython3.13"
  echo "   Fedora/RHEL:   sudo dnf install python3-devel"
  exit 1
fi

# ---- 2. 可选依赖（装了就能获得更好体验）------------------------------------
for pkg in onnxruntime tokenizers chromadb; do
  if "$PY" -c "import $pkg" 2>/dev/null; then
    echo "    ✓ $pkg"
  else
    echo "    ! $pkg 未安装 —— 打包产物将缺少对应能力"
  fi
done

# ---- 3. 清理并构建 --------------------------------------------------------
rm -rf build dist
"$PY" -m PyInstaller packaging/email-assistant.spec --noconfirm --log-level WARN

echo "==> 冒烟测试（隔离环境，验证产物自包含）"
SMOKE_HOME="$(mktemp -d)"
trap 'rm -rf "$SMOKE_HOME"' EXIT
BIN=./dist/email-assistant/email-assistant

# 注意：env -i 会清空整个环境，EMAIL_ASSISTANT_HOME 必须写在 env -i 的参数里，
# 写成 `VAR=x env -i ...` 会被 env -i 抹掉，导致烟测把数据写进包体。
ISO_ENV=(env -i "PATH=/usr/bin:/bin" "HOME=${SMOKE_HOME}" "EMAIL_ASSISTANT_HOME=${SMOKE_HOME}")

"${ISO_ENV[@]}" "$BIN" init --non-interactive >/dev/null
"${ISO_ENV[@]}" "$BIN" --version

echo "    检查关键能力"
"${ISO_ENV[@]}" "$BIN" doctor --json > "$SMOKE_HOME/doctor.json" 2>/dev/null || true

"$PY" - "$SMOKE_HOME/doctor.json" <<'PYCHECK'
import json, sys
checks = json.load(open(sys.argv[1], encoding="utf-8"))["checks"]
by_name = {c["name"]: c for c in checks}
required = [
    "SQLite 可用", "FTS5 全文检索", "数据库完整性",
    "归档目录可写", "数据库目录可写", "日志目录可写",
    "依赖 imap_tools", "依赖 fastapi", "依赖 cryptography",
    "依赖 bs4", "依赖 markdownify", "依赖 apscheduler",
]
failed = [n for n in required if not by_name.get(n, {}).get("ok")]
if failed:
    print("    X 关键检查未通过：", ", ".join(failed))
    sys.exit(1)
print("    OK 关键检查全部通过")
for n in ("可选依赖 onnxruntime", "可选依赖 tokenizers",
          "可选依赖 chromadb", "可选依赖 pystray"):
    c = by_name.get(n, {})
    print(f"      {'OK ' if c.get('ok') else '-- '} {n}")
PYCHECK
if [ $? -ne 0 ]; then
  echo "!! 冒烟测试失败，中止打包"
  exit 1
fi

# ---- 4. 打包 tar.gz -------------------------------------------------------
# 关键：绝不能把构建机上跑出来的配置/数据库/日志打进发行包
echo "==> 清理包内运行时残留"
rm -rf dist/email-assistant/config dist/email-assistant/data dist/email-assistant/logs
find dist/email-assistant -name "*.tmp" -delete 2>/dev/null || true

TARBALL="dist/email-assistant-${VERSION}-linux-${ARCH}.tar.gz"
echo "==> 打包 ${TARBALL}"
# 附带一份快速上手说明，解压即可看到
cat > dist/email-assistant/快速上手.txt <<'EOF'
腾讯企业邮箱邮件管理助手 —— Linux 版
=====================================

1. 初始化配置（会在本目录生成 config/config.yaml）
     ./email-assistant init

2. 设置授权码（输入不回显）
     ./email-assistant auth set
   或在腾讯企业邮箱 → 设置 → 客户端设置 生成授权码后，
   改用环境变量： export EMAIL_ASSISTANT_AUTH_CODE='...'

3. 自检
     ./email-assistant doctor --check-imap

4. 同步 / 检索 / 服务
     ./email-assistant sync
     ./email-assistant search "报销发票"
     ./email-assistant serve      # http://127.0.0.1:8990/docs

5. 托盘常驻（无图形界面时自动降级为守护模式）
     ./email-assistant tray

想先看看效果（不连邮箱）：
     ./email-assistant demo

启用语义检索（需要 ONNX 模型）：
     详见 README.md「启用真正的语义检索」

数据位置：本目录下的 config/ data/ logs/
可用 EMAIL_ASSISTANT_HOME 环境变量整体重定位。
EOF
tar -czf "$TARBALL" -C dist email-assistant
echo "    → $TARBALL ($(du -h "$TARBALL" | cut -f1))"

# ---- 5. AppImage（可选）---------------------------------------------------
if command -v appimagetool >/dev/null 2>&1; then
  echo "==> 构建 AppImage"
  APPDIR="dist/AppDir"
  rm -rf "$APPDIR"
  mkdir -p "$APPDIR/usr/bin"
  cp -r dist/email-assistant/* "$APPDIR/usr/bin/"

  cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/bash
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/email-assistant" "$@"
EOF
  chmod +x "$APPDIR/AppRun"

  # AppImage 强制要求：.desktop 里必须有 Icon=，且 AppDir 内确实存在同名图标
  # （.png/.svg/.xpm）。缺任一条件 appimagetool 会以
  #   "Icon entry not found in desktop file" 失败。
  cat > "$APPDIR/email-assistant.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=邮件管理助手
Name[en]=Email Assistant
Comment=腾讯企业邮箱邮件归档与知识库检索
Exec=email-assistant tray
Icon=email-assistant
Terminal=false
Categories=Office;Email;
EOF

  if [ ! -f packaging/icon.png ]; then
    echo "    ! 缺少 packaging/icon.png，正在生成"
    "$PY" packaging/make_icons.py >/dev/null 2>&1 || true
  fi
  if [ -f packaging/icon.png ]; then
    cp packaging/icon.png "$APPDIR/email-assistant.png"
  else
    echo "    ! 无法生成图标，AppImage 构建会失败"
  fi

  # 某些版本发布的 appimagetool 自身是 AppImage，执行依赖 FUSE；
  # 另一些版本是静态 ELF（continuous 现在如此）。为兼容两者：
  # 先直接运行，失败再解包运行。
  run_appimagetool() {
    if ARCH="$ARCH" appimagetool "$@" 2>/dev/null; then
      return 0
    fi
    echo "    (appimagetool 直接运行失败，尝试解包运行 —— 通常是缺少 FUSE)"
    if appimagetool --appimage-extract >/dev/null 2>&1 && [ -x squashfs-root/AppRun ]; then
      ARCH="$ARCH" ./squashfs-root/AppRun "$@"
      local rc=$?
      rm -rf squashfs-root
      return $rc
    fi
    return 1
  }

  APPIMAGE="dist/EmailAssistant-${VERSION}-linux-${ARCH}.AppImage"
  if run_appimagetool "$APPDIR" "$APPIMAGE"; then
    echo "    → $APPIMAGE"
  else
    echo "    ! AppImage 构建失败（不影响 tar.gz）"
  fi
else
  echo "    (跳过 AppImage：未安装 appimagetool)"
fi

echo
echo "==> 完成。产物在 dist/ 下。"
