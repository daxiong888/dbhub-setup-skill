# DBHub Setup Skill

[![skills.sh](https://skills.sh/b/daxiong888/dbhub-setup-skill)](https://skills.sh/daxiong888/dbhub-setup-skill/dbhub-setup)

[中国大陆用户可通过腾讯云 SkillHub 安装](https://skillhub.cn/skills/user_ad7d2e16/dbhub-setup)

Set up DBHub MCP in Codex for multiple local, test, and production databases. Passwords stay in macOS Keychain; SQL tools are read-only by default.

给 Codex 配置 [DBHub](https://github.com/bytebase/dbhub) MCP，接入本地、测试和生产环境中的多个数据库。密码保存在 macOS Keychain，SQL 工具默认只读。

这个仓库有两部分：

| 你想做什么 | 从哪里开始 |
| --- | --- |
| 在 Codex 项目里接入 DBHub | 安装 `skills/dbhub-setup/` |
| 自己检查 DBHub 官方稳定版 | clone 仓库，运行 `maintainer/` 下的工具 |

`maintainer/` 是仓库级工具，不会被 `skills.sh` 安装。clone 仓库后就可以运行，没有额外的身份限制。

> [!IMPORTANT]
> 目前验证过的组合是 **macOS + OpenAI Codex**，运行时需要 Python 3.10+ 和 Node.js（含 npm/npx）。Windows、Linux 和其他 Agent 需要换掉 Keychain 与项目级 MCP 配置，并重新测试。

> [!WARNING]
> `readonly = true` 是 DBHub 的应用层限制，不能代替数据库只读账号。MCP 能启动，也不代表已经连上真实数据库。

## 安装 Skill

### 中国大陆

SkillHub 提供国内下载。将下面这句话发给支持 Skill 安装的 AI 助手：

```text
请根据 https://skillhub.cn/install/skillhub.md，安装 @user_ad7d2e16/dbhub-setup。
```

也可以先查看 [SkillHub 上的 dbhub-setup](https://skillhub.cn/skills/user_ad7d2e16/dbhub-setup)。

### GitHub / skills.sh

临时加载并立即使用：

```text
Run `npx skills use "https://github.com/daxiong888/dbhub-setup-skill" --skill "dbhub-setup"` and follow the generated skill instructions now. Read its complete output, redirecting it to a temporary file first if necessary. Resolve relative paths from the supporting-files directory it provides.
```

需要持久安装到 Codex 时：

```bash
npx skills add daxiong888/dbhub-setup-skill \
  --skill dbhub-setup \
  --agent codex \
  --global
```

只查看仓库里有哪些 Skill：

```bash
npx skills add daxiong888/dbhub-setup-skill --list
```

安装后可以在目标项目里这样说：

```text
使用 $dbhub-setup，先检查这个项目现有的 Codex 和 DBHub 配置，
再为 LOCAL、UAT 和 PROD 的多个数据库生成 dry-run。
不要读取、输出或保存任何数据库密码。
```

Skill 会先检查 Git 状态和已有配置，再收集数据库类型、地址、库名、用户名、环境名等非敏感信息。确认 dry-run 后，它会生成：

```text
<project>/.codex/config.toml
<project>/.codex/dbhub/dbhub.toml
<project>/.codex/dbhub/start-dbhub.zsh
```

密码不写入这些文件。启动器从 macOS Keychain 读取密码，每个数据源默认启用 `lazy = true`；`execute_sql` 默认是只读模式，并限制单次返回行数。生成器还会把 `.codex/` 加入当前仓库的 `.git/info/exclude`，避免本地数据库地址和用户名误入提交。

除非你明确授权，Skill 不会连接 UAT、生产环境或其他私有数据库。它也不会创建数据库账号、修改授权或执行迁移。

## 当前验证版本

公开 Skill 默认使用：

```text
@bytebase/dbhub@1.2.0
```

仓库中的资格矩阵已经检查过 DBHub `1.2.0` 在 SQLite、PostgreSQL、MySQL、MariaDB 和 SQL Server 上的核心行为。项目生成器也允许指定其他精确的 `X.Y.Z` 稳定版本，但会将其标记为未验证。

`latest`、版本范围、预发布版本、URL 和任意 npm spec 都不会被接受。启动器只使用官方 npm registry，并禁用 npm lifecycle scripts；首次启动时可能需要联网下载精确版本。

## 自己检查 DBHub 稳定版

先 clone 仓库：

```bash
git clone https://github.com/daxiong888/dbhub-setup-skill.git
cd dbhub-setup-skill
```

资格工具的 Node.js 依赖锁在 `package-lock.json` 中：

```bash
cd maintainer/qualification
npm ci --ignore-scripts
cd ../..
```

只查询官方 npm 元数据，并检查本地是否已有可复用的资格凭据：

```bash
python3 maintainer/scripts/manage_dbhub_release.py check
```

这条命令不会下载 DBHub，也不会启动 Docker。想在需要时继续跑完整矩阵，使用：

```bash
python3 maintainer/scripts/manage_dbhub_release.py qualify
```

`qualify` 也会先做轻量检查。只有发现新稳定版、当前资格输入发生变化，或之前的运行没有完成时，才会下载精确的官方 npm artifact 并启动本地矩阵。

完整矩阵需要本地 Docker，以及 [dbhub_release_matrix.json](maintainer/scripts/dbhub_release_matrix.json) 中列出的镜像。脚本不会自行执行 `docker pull`。缺少镜像时会返回 `blocked`，由你决定是否手动准备对应的精确镜像。

常见状态：

| 状态 | 含义 |
| --- | --- |
| `up_to_date` | 已有资格凭据可以复用，没有重跑矩阵 |
| `qualification_required` | 需要运行完整矩阵 |
| `qualified` | 当前矩阵全部通过 |
| `partially_qualified` | 只有部分数据库 connector 通过 |
| `blocked` | 缺少镜像、依赖或其他本地条件 |
| `security_blocked` | 同版本的官方制品身份发生异常变化 |

检查过程不读取业务项目或 macOS Keychain，也不连接 UAT、生产环境和其他私有数据库。详细命令、定期检查方式和旧版项目升级工具见 [maintainer/README.md](maintainer/README.md)。

## 测试

公开 Skill：

```bash
cd skills/dbhub-setup/tests
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -v
```

仓库级资格工具：

```bash
cd maintainer/tests
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -v
```

GitHub Actions 会运行这两组测试。完整数据库矩阵不会在普通单元测试中启动。

## 已验证范围

| 项目 | 状态 |
| --- | --- |
| macOS | 已验证 |
| OpenAI Codex | 已验证 |
| DBHub `1.2.0` | 已验证 |
| 多环境、多数据库项目配置 | 已验证 |
| macOS Keychain 凭据加载 | 已验证 |
| SQLite、PostgreSQL、MySQL、MariaDB、SQL Server 资格矩阵 | 已验证 |
| Windows / Linux 凭据存储 | 未验证 |
| Claude Code / Cursor / 其他 Agent | 未验证 |

## 移植到其他平台

移植时，凭据仍要交给系统安全存储；配置先 dry-run，真实数据库连接单独授权。之后再把 Keychain 和 Codex 项目配置换成目标平台的实现。

## 项目说明

这是社区维护的独立 Skill，不是 Bytebase 或 DBHub 官方项目。DBHub 名称及相关权利归其各自权利人所有。

## License

[MIT](LICENSE)
