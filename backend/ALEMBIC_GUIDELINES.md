# 数据库初始化与迁移

工程环境与验证规则以 `.agents/architecture/environments-and-operations.md`
和 `.agents/rules/deploy.md` 为准。所有命令在 Docker 内执行，测试使用独立
PostgreSQL 数据库，不在共享开发库或生产库重放迁移。

## 当前版本初始化

在挂载当前 backend 的测试容器中运行：

```sh
python -m app.scripts.bootstrap_db
```

容器 entrypoint 调用此命令。在线 CLI `alembic upgrade head` 和
`alembic upgrade heads` 也通过同一个 `alembic/env.py` 初始化边界：

- 真正空库：完整模型 registry 建表、安装 database guards、stamp heads，
  在一个事务中完成；不重复执行历史 ADD COLUMN。
- 有 Alembic 版本的库：运行增量升级，不先执行当前 metadata.create_all。
- 有业务表但无版本的库：拒绝自动升级或 stamp，保留数据并报告原因。
  必须先确认其真实遗留版本；不能因为表存在就假定结构完整。

PostgreSQL 会话 advisory lock 防止多个 bootstrap 同时修改 schema。
历史 revision 目标、downgrade、stamp、autogenerate 和离线 SQL 不使用空库
当前 schema 快捷路径。程序化调用应使用 bootstrap 模块；不要复制入口判断。

## 修改模型与迁移

新迁移采用 `<YYYYMMDDHHMM>_<description>.py` 命名；每个手写源文件不超过
800 物理行。模型、迁移和必要行为验收作为一个完整变更审查。

1. 修改对应模型；新模块加入 `app/models/registry.py`。
2. 在隔离 Docker 环境生成迁移，人工检查 DDL、数据回填、锁与事务边界。
3. 可由 ORM 表达的约束、普通索引、部分/表达式索引同步加入 metadata；
   trigger/function 复用 `app/core/database_guards.py`。
4. 验证空库初始化、相同库重复运行、受支持旧版本升级。Alembic 版本正确
   不等于结构完整：通过真实 PostgreSQL 写入验证唯一性、租户边界和外键。
5. 已经 stamp 到旧 heads 却遗漏对象的安装，通过新 repair revision 修复；
   不修改历史迁移来绕过当前模型与历史 ADD 的碰撞。

既有大表索引沿用并发构建、短 lock timeout 和 invalid-index 重试处理。
唯一索引遇到冲突数据必须失败；不得自动合并或删除身份数据来让迁移变绿。
只修复原本应存在的索引时，downgrade 不应移除更早版本也要求的约束。

## 验证入口

按环境文档准备已隔离的 Docker 测试数据库和完整挂载后运行：

```sh
python -m pytest tests/test_bootstrap_db.py tests/test_database_guards.py -q -p no:cacheprovider
```

bootstrap 测试还会创建并清理自己的 `test_bootstrap_*` 数据库，因此隔离测试
PostgreSQL 角色需要 CREATEDB 权限。该权限仅用于测试，不要求生产应用角色
具有 CREATEDB。

用固定的真实旧 schema/数据验证跨版本升级；不要把当前 metadata stamp 成
任意旧 revision 伪造升级覆盖。测试不得复制生产迁移算法、匹配源码字符串，
或用 SQLite 代替 PostgreSQL 约束验收。
