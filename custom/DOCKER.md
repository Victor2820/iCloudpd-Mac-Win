# 定制版 icloudpd 容器镜像

镜像由 `.github/workflows/docker-build.yml` 构建，推送到 GitHub Container Registry：

```
ghcr.io/victor2820/icloudpd:latest        # 最新一次构建
ghcr.io/victor2820/icloudpd:1.32.3        # 按上游版本
ghcr.io/victor2820/icloudpd:1.32.3-b<N>   # 按构建号，锁死用这个
```

平台：`linux/amd64`、`linux/arm64`（manifest 多架构，`docker pull` 自动挑）。

镜像里装的是 Python 包而不是 PyInstaller 单文件二进制 —— 上游依赖全部有 wheel，
一个 Dockerfile 就能覆盖两个架构，不需要交叉工具链。

## 基本用法

```sh
docker run -it --rm \
  -v $(pwd)/Photos:/data \
  -v $(pwd)/cookies:/cookies \
  -e TZ=Asia/Shanghai \
  ghcr.io/victor2820/icloudpd:latest \
  --username you@example.com \
  --cookie-directory /cookies \
  --directory /data \
  --watch-with-interval 3600
```

Windows 上把 `$(pwd)` 换成 `%cd%`，或写完整路径 `-v c:/photos:/data`。

**`--cookie-directory` 必须挂在卷上。** 默认写在 `~/.pyicloud`，也就是容器的可写层，
容器一删会话就没了 —— 下次启动又要一次完整的登录和短信验证码。挂到卷上之后，
只要会话没过期就直接开始同步。

## 登录与验证码

第一次跑（或会话过期后）会要密码和双重认证验证码，所以要带 `-it`，否则
`getpass` 读不到终端输入，会一直卡住。

- 密码：终端里输入，或用 `--password-provider console` 强制走终端。
  引导模式不会去碰系统钥匙串（容器里也没有），这点刻意保留。
- 验证码：收到短信后在同一终端里输入。

不想每次交互就给密码的，可以用 `--password` 直接传（但密码会出现在
`docker inspect` 和 shell 历史里，不建议）。

## 容器里的两个行为差异

**不会卡在「按任意键退出」。** 桌面版结束时会暂停等你按键，免得从文件管理器
打开的窗口一闪就没了。容器里没人按键，所以 entrypoint 在**有参数**时自动补
`--no-pause`；只在**完全不带参数**跑引导向导时才保留暂停。

**两个命令都能用。** `docker run <image> icloud --help` 走 pyicloud 的命令行，
其余一律当 icloudpd 的参数 —— 也就是 `docker run <image> --directory /data ...`
这种省略命令名的写法同样有效（上游镜像必须先写 `icloudpd`）。

## 文件权限

容器以 root 运行，下载的照片在宿主机上也归 root。想让它们属于自己：

```sh
docker run --user $(id -u):$(id -g) ...
```

这样 cookie 卷和照片目录都得让这个 uid 可写 —— 先 `chown` 一下，否则会
permission denied。

## 定时同步

`--watch-with-interval 3600` 是进程内循环（容器一直活着）。
更喜欢由外部调度的话，去掉它，用 cron / systemd timer 定期 `docker run` 一次，
配合挂在卷上的 cookie 目录，只有第一次需要验证码。

## 本地构建

在**本仓库根目录**（不是 custom/ 里）：

```sh
docker build -f custom/Dockerfile -t icloudpd .
docker buildx build -f custom/Dockerfile --platform linux/amd64,linux/arm64 -t icloudpd .
```

构建时只需要 `custom/` 这一个目录的内容；上游源码是 Dockerfile 自己按 pin 的
commit 拉的。

## 想推到 Docker Hub 而不是 GHCR

GHCR 用 workflow 自带的 `GITHUB_TOKEN` 就能推，不需要额外配置。要推 Docker Hub，
在仓库 Settings → Secrets 里加 `DOCKERHUB_USERNAME` 和 `DOCKERHUB_TOKEN`，
然后在 workflow 里加一步 `docker/login-action`（registry 留空即 Docker Hub），
并把 tags 换成 `<用户名>/icloudpd:...`。
