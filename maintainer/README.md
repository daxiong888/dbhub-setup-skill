# DBHub release qualification

这里放的是仓库级版本检查工具。`skills.sh` 只安装 `skills/dbhub-setup/`，所以发布检查留在源码仓库中。clone 仓库并准备好本地依赖后，任何人都可以运行。

## 当前基线

- 公开 Skill 默认使用 `@bytebase/dbhub@1.2.0`；
- 资格检查目前在 macOS 上验证；
- 本地矩阵包含 SQLite、PostgreSQL、MySQL、MariaDB 和 SQL Server；
- 资格凭据默认保存在 `~/.codex/state/dbhub-setup`。

## 准备依赖

从仓库根目录执行：

```bash
cd maintainer/qualification
npm ci --ignore-scripts
cd ../..
```

完整矩阵还需要本地 Docker，以及 [dbhub_release_matrix.json](scripts/dbhub_release_matrix.json) 中列出的精确镜像。脚本不会自动拉取镜像，也不会删除本机已有的镜像、容器或数据卷。

## 检查官方稳定版

只读官方 npm 元数据和本地资格状态：

```bash
python3 maintainer/scripts/manage_dbhub_release.py check
```

`check` 不下载包，也不启动 Docker。需要时继续跑完整矩阵：

```bash
python3 maintainer/scripts/manage_dbhub_release.py qualify
```

`qualify` 会先执行同样的轻量检查。已有凭据可以复用时，结果是 `up_to_date`；需要重新验证时，它才会下载精确的官方 artifact 并启动矩阵。缺少本地镜像会返回 `blocked`，不会悄悄执行 `docker pull`。

如果只想复现某个稳定版本：

```bash
python3 maintainer/scripts/manage_dbhub_release.py \
  qualify \
  --version 1.2.0
```

检查不会扫描业务项目，不读取 macOS Keychain，也不连接 UAT、生产环境或其他私有数据库。

## 定期检查

定期任务直接调用这里的脚本，不需要公开 `$dbhub-setup` Skill：

```bash
python3 /absolute/path/to/dbhub-setup-skill/maintainer/scripts/manage_dbhub_release.py \
  qualify \
  --registry https://registry.npmjs.org \
  --state-dir ~/.codex/state/dbhub-setup
```

定期任务不要使用 `--force`，不要自动拉取镜像、修改业务项目或更新公开版本。

`qualified` 表示候选版本通过了当前矩阵，可以进入人工复核。它不应该直接触发 `VERIFIED_DBHUB_VERSION`、README、GitHub Release 或已安装 Skill 的更新。`partially_qualified` 表示只有部分 connector 通过，不满足当前公开默认版本的完整资格门。

## 更新公开默认版本

完成资格检查和测试后，再统一修改：

- `skills/dbhub-setup/scripts/setup_dbhub_project.py` 中的 `VERIFIED_DBHUB_VERSION`；
- 根目录 README；
- `skills/dbhub-setup/SKILL.md`；
- Changelog 和 Release 信息。

这些修改需要人工复核，不属于定期任务。

## 测试

```bash
cd maintainer/tests
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -v
```

`upgrade_dbhub_project.py` 和 `run_qualified_dbhub.py` 用来处理早期采用资格运行时的项目。新安装的公开 Skill 不依赖它们。
