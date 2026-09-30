#!/usr/bin/env bash
#
# 本机开发用的 pgvector 容器管理脚本。
#
#   ./scripts/pg_local.sh up      启动容器并等待就绪，打印测试用 DSN
#   ./scripts/pg_local.sh dsn     只打印 DSN
#   ./scripts/pg_local.sh psql    进容器里的 psql
#   ./scripts/pg_local.sh logs    看日志
#   ./scripts/pg_local.sh down    停止并删除容器（保留数据卷）
#   ./scripts/pg_local.sh nuke    连数据卷一起删除
#
# 这只是**本机验证**环境：真实执行环境是远程服务器上的原生 PostgreSQL，
# 见 doc/15_memory_postgres_deployment.md。不要用这个容器承载业务数据。
#
# 端口默认 15432。刻意避开两个坑：
#   * 5432——将来本机装原生 PostgreSQL 会冲突；
#   * 5xxxx 段（如 55432）——Windows 会保留大段动态端口
#     （用 `netsh int ipv4 show excludedportrange protocol=tcp` 查），
#     落进去会报 "bind: An attempt was made to access a socket in a way
#     forbidden by its access permissions"。

set -euo pipefail

cmd="${1:-up}"

name="${KLONET_PG_CONTAINER:-klonet-pgvector}"
port="${KLONET_PG_PORT:-15432}"
password="${KLONET_PG_PASSWORD:-klonet}"
image="${KLONET_PG_IMAGE:-pgvector/pgvector:pg17}"
volume="${KLONET_PG_VOLUME:-klonet-pgvector-data}"

dsn="postgresql://postgres:${password}@127.0.0.1:${port}/postgres"

require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "docker 命令不可用，请先安装 Docker Desktop。" >&2
    exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "Docker 守护进程没有运行。" >&2
    echo "请手动启动 Docker Desktop（GUI 程序无法由脚本代启），再重试。" >&2
    exit 1
  fi
}

case "$cmd" in
  up)
    require_docker
    if docker ps --format '{{.Names}}' | grep -qx "$name"; then
      echo "容器 $name 已在运行。"
    elif docker ps -a --format '{{.Names}}' | grep -qx "$name"; then
      echo "容器 $name 已存在但未运行，启动它……"
      docker start "$name" >/dev/null
    else
      echo "创建容器 $name（$image）……"
      docker run -d \
        --name "$name" \
        -p "${port}:5432" \
        -e POSTGRES_PASSWORD="$password" \
        -v "${volume}:/var/lib/postgresql/data" \
        "$image" >/dev/null
    fi

    echo "等待 PostgreSQL 就绪……"
    for _ in $(seq 1 60); do
      if docker exec "$name" pg_isready -U postgres >/dev/null 2>&1; then
        break
      fi
      sleep 1
    done

    if ! docker exec "$name" pg_isready -U postgres >/dev/null 2>&1; then
      echo "等待超时，用 ./scripts/pg_local.sh logs 看容器日志。" >&2
      exit 1
    fi

    # pgvector 镜像默认已装扩展，这里再确认一次，避免用到残缺镜像时后知后觉。
    if ! docker exec "$name" psql -U postgres -tAc \
        "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'" | grep -q 1; then
      echo "警告：该镜像里没有 pgvector 扩展，请换用 pgvector/pgvector 镜像。" >&2
      exit 1
    fi

    echo
    echo "就绪。测试用 DSN："
    echo "  export KLONET_AGENT_TEST_PG_DSN=\"$dsn\""
    echo
    echo "跑验收："
    echo "  python -m pytest tests/test_memory_postgres_repository.py -q"
    ;;

  dsn)
    echo "$dsn"
    ;;

  psql)
    require_docker
    docker exec -it "$name" psql -U postgres
    ;;

  logs)
    require_docker
    docker logs --tail 80 "$name"
    ;;

  down)
    require_docker
    docker rm -f "$name" >/dev/null 2>&1 || true
    echo "已删除容器 $name（数据卷 $volume 保留）。"
    ;;

  nuke)
    require_docker
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker volume rm "$volume" >/dev/null 2>&1 || true
    echo "已删除容器 $name 与数据卷 $volume。"
    ;;

  *)
    echo "用法：$0 {up|dsn|psql|logs|down|nuke}" >&2
    exit 2
    ;;
esac
