#!/usr/bin/env bash
#
# 一键克隆 deepexplor-services 全部 13 个仓库到当前目录
# ---------------------------------------------------------------
# 用法：
#   1) 在新设备上先配好 SSH key 并加到 GitHub：
#        ssh-keygen -t ed25519 -C "你的标识" -f ~/.ssh/id_ed25519 -N ""
#        cat ~/.ssh/id_ed25519.pub      # 复制输出，粘到 https://github.com/settings/ssh/new
#        ssh -T git@github.com          # 看到 "Hi KevinJH82!" 说明成功
#   2) 把本脚本放到你想存放代码的目录里，执行：
#        bash clone-all.sh
#
# 注意：克隆下来的只有代码，数据/venv/下载/模型权重/缓存均不在其中
#       （它们已被 .gitignore 排除），需在新设备上自行重建。
# ---------------------------------------------------------------

set -u
GH_USER="KevinJH82"

REPOS=(
  commons
  data-colle
  geo-exploration
  geo-insar
  geo-reporter
  geo-stru
  documents
  geo-downloader
  geo-analyser
  geo-model3d
  geo-geochem
  geo-geophys
  geo-drill
)

echo "==> 开始克隆 ${#REPOS[@]} 个仓库（用户: ${GH_USER}）"
echo

ok=0; skip=0; fail=0
for r in "${REPOS[@]}"; do
  if [ -d "$r/.git" ]; then
    echo "[跳过] $r  已存在，改为拉取最新： git -C $r pull"
    git -C "$r" pull --ff-only && skip=$((skip+1)) || fail=$((fail+1))
  else
    echo "[克隆] $r"
    if git clone "git@github.com:${GH_USER}/${r}.git"; then
      ok=$((ok+1))
    else
      echo "  !! $r 克隆失败（检查 SSH key 是否已加到 GitHub）"
      fail=$((fail+1))
    fi
  fi
  echo
done

echo "==> 完成：新克隆 ${ok}，已存在并更新 ${skip}，失败 ${fail}"
[ "$fail" -eq 0 ] && echo "全部 OK ✅" || echo "有 ${fail} 个失败，请看上面日志 ⚠️"
