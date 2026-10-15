# icloudpd 定制版构建仓库

这个仓库**不存上游源码**，只存与上游 `icloud_photos_downloader` 的差异。构建时
workflow 会按 pin 的 commit 拉上游 tarball，把差异打进去，再用 PyInstaller 打包。

上游 pin：`icloud-photos-downloader/icloud_photos_downloader` @ `879c561`（v1.32.3），
写在三个 workflow 的 `env:` 块里（改 pin 必须三个一起改）。

## 内容

| 路径 | 作用 |
|---|---|
| `custom/icloudpd-custom.patch` | 对上游 6 个源文件的改动（`git apply -p1`） |
| `custom/guided_setup.py` | 新增：中文引导向导 |
| `custom/remember.py` | 新增：记住上次成功的 Apple ID 与目录（不存密码） |
| `custom/build_mac_bin.sh` | macOS 打包脚本 |
| `scripts/upstream_sync.py` | 上游发新版时的 rebase 工具 |
| `.github/workflows/build-win-exe.yml` | Windows x64 单文件 exe |
| `.github/workflows/build-linux-bin.yml` | Linux x86_64 单文件可执行程序（ubuntu-22.04 / glibc 2.35） |
| `.github/workflows/build-mac-bin.yml` | macOS arm64 + x86_64 单文件可执行程序 |
| `.github/workflows/upstream-sync.yml` | 每周检查上游是否有新版 |

## 定制点

1. 不带参数运行 → 中文引导向导（Apple ID / 密码 / 区域 / 目录）；带参数 → 等价原版。
2. `--live-photo-mov-filename-policy` 默认 `original`（上游 `suffix`）。
3. 记住上次成功的用户名 + 目录；`--no-remember` 双向关闭。
4. 索引未完成时**轮询等待**（5s 起、翻倍、封顶 30s，默认 300 秒），
   环境变量 `ICLOUDPD_INDEXING_TIMEOUT` 覆盖，设 `0` 恢复上游旧行为。
5. `--ignore-indexing-state` 把索引类错误降级为警告并放行（与 `--auto-delete`、
   `--keep-icloud-recent-days` 互斥）；`records` 为空仍报错。
6. 修复上游 v1.32.3 的退出码问题：PyInstaller 二进制原来任何失败都退出 0。

## 怎么触发构建

三个 workflow 都有 `workflow_dispatch`，在仓库 Actions 页面选对应 workflow
点 `Run workflow` 即可（分支选 `main`）。产物会挂到 release 上。
推到 `main` 且改动落在 `custom/` 或 workflow 文件时也会自动触发。

## 改动后要注意

- 改任一个被打补丁的文件，都要重新生成 `custom/icloudpd-custom.patch`，
  并同步三个 workflow 里 "Verify" 步骤的 sha256。
- workflow 的 Verify 步骤**必须**保持 `echo "<hash>  <path>" | sha256sum -c -`
  的逐行写法，`scripts/upstream_sync.py` 靠正则匹配这些行来更新哈希。
- `scripts/upstream_sync.py` 目前只 rebase `src/icloudpd/cli.py`；补丁一旦包含
  其他文件，它会直接报错退出，防止静默丢定制。真要同步新版本时先泛化它。
