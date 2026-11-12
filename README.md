# 机队观测接收与质量处理

设备别名和校准按有效期解析，异常观测进入隔离区。工单、分区水位和成品发布使用独立状态，不把收到数据等同于可交付。

运行需要 Python 3.11 或更高版本，使用标准库与 SQLite，无需第三方包。演练中的机构、人员、交易及读数全部为本地合成数据。

## 构建与测试

```sh
python3 -m compileall -q hangar_ingest
python3 -m unittest discover -s tests -v
```

## 本地运行

```sh
python3 -m hangar_ingest --db :memory: demo --fixture fixtures/demo.json
python3 -m hangar_ingest --help
```

演练使用临时数据库，不会连接远程服务。命令以 JSON 显示业务结果；测试覆盖核心状态、边界输入和错误路径。运行前可用 `python3 --version` 核对解释器版本。
