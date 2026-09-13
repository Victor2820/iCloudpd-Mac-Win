# 上游更新时怎么办

这个仓库**不存上游源码**，只存与上游的差异。所以「同步上游」＝让下面三样东西一起往前走：

| 要动的东西 | 在哪 |
| --- | --- |
| 四个 pin（上游 commit、版本号、短 sha、时间戳） | 两个 build workflow 的 `env:` 块 |
| 补丁本身，要 rebase 到新的 `src/icloudpd/cli.py` 上 | `custom/icloudpd-custom.patch` |
| Verify 步骤里断言的那三个 sha256 | 两个 build workflow 的 Verify 步骤 |

**这三样必须一起动。** 只改 pin 不改补丁 → 补丁打不上，构建直接失败；只改补丁不改 sha256 → 校验步骤失败。构建挂了不算坏事（不会发出错误的包），但白跑一趟。

## 自动化（默认开着）

`.github/workflows/upstream-sync.yml` 每周一 UTC 03:17（北京时间 11:17）检查一次上游有没有新 release。

- **补丁能干净 rebase** → 自动提交 pin 和补丁，然后触发 mac + Windows 两个构建。
- **补丁冲突** → 什么都不提交，**开一个 issue** 并把冲突内容贴进去，同时这次运行标红。

想手动跑一次：仓库 **Actions** → `Sync with upstream` → **Run workflow**（可填一个指定的 tag 或 commit，留空则取最新 release）。

不想让它自动提交，就把 workflow 里的 `schedule:` 两行删掉 —— 之后只有手动触发才跑。

## 手动跑（本地）

```bash
# 1. 先看看有没有新版本（只检查，不改任何东西）
python scripts/upstream_sync.py --check

# 2. 正式同步
python scripts/upstream_sync.py

# 3. 或者指定某个版本
python scripts/upstream_sync.py --to v1.33.0

# 4. 不想写盘，先看它打算改什么
python scripts/upstream_sync.py --dry-run
```

它做的事，按顺序：

1. 读两个 workflow，拿到当前 pin；
2. 查上游最新 release（或 `--to` 指定的 ref），解析成具体 commit；
3. 下载新旧两个上游快照；
4. 用 **三路合并** rebase 补丁 —— `base` 是旧上游的 `cli.py`，`ours` 是我们打过补丁的，`theirs` 是上游新版的。上游改了但没碰到我们那几段，就自动带过去；真撞上了就报冲突；
5. 静态检查我们新增模块 import 的上游符号（`GlobalConfig`、`UserConfig`、`AssetVersionSize` 等）在新版里是否还在；
6. 生成新补丁、算新 sha256、改两个 workflow；
7. **最后再验一次**：把新补丁打到新版上游的干净副本上，确认产出的字节和预期 sha256 一致。对不上就不写盘。

退出码：`0` 成功或已是最新，`3` 冲突，`1` 其他错误。

## 冲突了怎么收尾

冲突不会自动猜解，只会停下来 —— 这是故意的，猜错的代价是发出去一个行为不对的二进制。它会留一个 `cli.py.conflict`（带 `<<<<<<<` 标记），人工编辑后再交回去：

```bash
python scripts/upstream_sync.py --to v1.33.0            # 冲突，生成 cli.py.conflict
# 编辑 cli.py.conflict，把冲突标记解决掉
python scripts/upstream_sync.py --to v1.33.0 --resolve cli.py.conflict
```

`--resolve` 会校验文件里没有残留的冲突标记，然后接着走完剩下的步骤。

已知的常撞位置：**import 块**。我们的补丁挨着 import 区插入 `from icloudpd import remember` 等几行，而上游改 import 区并不罕见（v1.32.2 → v1.32.3 就加了 `from foundation.string_utils import lower`）。这类冲突很好解 —— 通常是两边各要自己的行，保留双方即可。

## 本地和仓库的关系

本地这份完整源码树里也有一个 `custom/`，内容和仓库里的一致，作用是让上面这些命令在本地也能直接跑（本地是完整 checkout，还多一份 `src/icloudpd/cli.py` 镜像，工具会一并更新，这样本地打出来的包和 CI 打的完全一致）。

改了 `custom/` 下的文件，推上去即可；两个 build workflow 会自动重新构建。
