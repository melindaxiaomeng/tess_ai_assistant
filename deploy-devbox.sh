#!/usr/bin/env bash
# =============================================================================
# Tess · 测试服务器（2vCPU / 2GiB / Alibaba Cloud Linux 3）一键部署
#
# 用法（在云机器上、已含本项目的目录内执行）：
#   1) 把代码上传到云机器（见 DEPLOY.md §10.2）
#   2) ssh 登录后 cd 到项目目录
#   3) 可选：export TESS_LLM_API_KEY=sk-xxx   # 诊断需要；健康检查/GAID 演练不需要
#   4) bash deploy-devbox.sh
#
# 脚本会自动：装 Docker -> 生成 .env -> docker compose 构建启动 -> 健康检查
# =============================================================================
set -euo pipefail

echo "=== Tess 测试服务器一键部署（2vCPU/2GiB）==="

# ---- 1. 安装 Docker（Alibaba Cloud Linux 3 / RHEL 系，dnf）----
if ! command -v docker >/dev/null 2>&1; then
  echo "[1/4] 未检测到 Docker，开始安装 ..."
  if command -v dnf >/dev/null 2>&1; then
    # Alibaba Cloud Linux 等 RHEL 系：dnf install docker 会装成 podman 垫片，
    # 且官方源 download.docker.com 国内连不上 → 改用阿里云镜像
    sudo dnf remove -y podman-docker 2>/dev/null || true
    if ! rpm -q docker-ce >/dev/null 2>&1; then
      sudo dnf -y install yum-utils
      sudo yum-config-manager --add-repo https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo 2>/dev/null || \
      sudo yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
      sudo sed -i 's/\$releasever/8/g' /etc/yum.repos.d/docker-ce.repo 2>/dev/null || true
      sudo dnf -y install docker-ce docker-ce-cli containerd.io docker-compose-plugin
    fi
  fi
  # 兜底：官方便捷脚本（Ubuntu/CentOS 通用）
  if ! command -v docker >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sudo sh
  fi
  sudo systemctl enable --now docker
  sudo usermod -aG docker "$USER" 2>/dev/null || true
  echo "      Docker 已安装。若提示权限问题，请 exit 重登后再执行本脚本。"
else
  echo "[1/4] Docker 已存在，跳过安装。"
fi

# ---- 2. 生成 .env ----
if [ ! -f .env ]; then
  echo "[2/4] 生成 .env（从 .env.example）..."
  cp .env.example .env
  if [ -n "${TESS_LLM_API_KEY:-}" ]; then
    sed -i "s#^TESS_LLM_API_KEY=.*#TESS_LLM_API_KEY=${TESS_LLM_API_KEY}#" .env
    echo "      已写入 TESS_LLM_API_KEY（来自环境变量）。"
  else
    echo "      ⚠️ 未设置 TESS_LLM_API_KEY。请编辑 .env 填入后重跑脚本。"
    echo "        诊断接口需要 Key；但 /healthz 与 GAID 隐私演练（resolve/delete）无需 Key，可先验证链路。"
  fi
else
  echo "[2/4] .env 已存在，跳过。"
fi

# ---- 3. 构建并启动 ----
echo "[3/4] docker compose 构建并后台启动 ..."
docker compose up -d --build

# ---- 4. 健康检查 ----
echo "[4/4] 等待 /healthz 就绪（最多 60s）..."
for i in $(seq 1 30); do
  if curl -fsS http://localhost:8080/healthz >/dev/null 2>&1; then
    echo "✅ 服务已就绪："
    curl -fsS http://localhost:8080/healthz
    echo
    echo "👉 测试前端把「后端地址」填为： http://<本机公网IP>:8080"
    echo "   查看日志： docker compose logs -f tess"
    echo "   停止服务： docker compose down"
    exit 0
  fi
  sleep 2
done
echo "❌ 超时未就绪。排查： docker compose logs tess"
exit 1
