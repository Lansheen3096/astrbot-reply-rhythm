# astrbot-reply-rhythm

> **本仓库已归档。三个插件已拆成独立仓库，请到那边装、提 issue、看文档。**

| 插件 | 仓库 | 版本 | 作用 |
|---|---|---|---|
| `astrbot_plugin_burst` | https://github.com/Lansheen3096/astrbot_plugin_burst | `v0.2.1` | 消息防抖聚合：连发的几条合成一条再交给模型 |
| `astrbot_plugin_pacer` | https://github.com/Lansheen3096/astrbot_plugin_pacer | `v0.2.2` | 分段投递 + 插话打断：插话就停发，历史截成实际发出的部分 |
| `astrbot_plugin_proactive` | https://github.com/Lansheen3096/astrbot_plugin_proactive | `v0.2.0` | 条件式主动消息：用户安静够久，bot 自己发一条 |

```bash
cd <AstrBot>/data/plugins
git clone --depth 1 --branch v0.2.1 https://github.com/Lansheen3096/astrbot_plugin_burst
git clone --depth 1 --branch v0.2.2 https://github.com/Lansheen3096/astrbot_plugin_pacer
git clone --depth 1 --branch v0.2.0 https://github.com/Lansheen3096/astrbot_plugin_proactive
# docker 部署：拷进宿主机挂载的数据目录后重启容器
docker compose restart astrbot
```

**依赖关系**：`proactive` 独立。`burst` 与 `pacer` 互相依赖（`pacer` 对外暴露
「这一轮在飞吗 / 已经发出去几段」，`burst` 拿它决定新消息排队还是打断）——
要么两个都装，要么都不装；只装一个时另一个的能力缺一半，代码里做了降级，不报错但语义不完整。

**0.1.0 → 0.2.0 更名**：`astrbot_plugin_flush` 已更名为 `astrbot_plugin_pacer`。
升级前**先删掉旧目录** —— 插件名换了框架不认旧名，两份同时放着会重复接管发送。

市场里搜「消息防抖聚合」「分段发送打断」「条件式主动消息」也能装，
但版本组合以独立仓库的 tag 为准。长期记忆那套（写入 + 注入 + 复核台）在
[astrbot-memory-layer](https://github.com/Lansheen3096/astrbot-memory-layer)。

历史上这个仓库把三个插件放在一个 monorepo 里；拆分的理由是各自发版、各自进市场，
不用为了改一个插件的版本号动另外两个。旧代码仍然在 git 历史里。

## License

MIT
