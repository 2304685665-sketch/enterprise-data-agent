---
name: internal-doc-citation
description: 使用 internal_doc_search 的结果并规范标注内部文档来源
triggers: 内部文档, 备忘录, 会议纪要, 投资, 产品介绍, 公司简介, 知识库, internal, memo, meeting notes
---
1. internal_doc_search 返回的每个片段以 `[Source: <doc_id>, section: <section>, ...]` 开头；引用时只能使用你在工具结果里真正看到的 doc_id。
2. 在结论里用 `(来源: <doc_id>)` 标注，且标注必须对应你确实读到并用到的片段；不要引用没有出现在结果里的文档编号。
3. 结果为空或片段与问题无关时，直接说明内部知识库中没有找到相关内容，不要猜测或改用无关片段拼凑。
4. relevance_distance 越小表示向量越接近，但它只说明"检索相似"，不代表片段一定回答了问题；用之前先读内容。
5. 同一问题需要多份文档佐证时，分别标注各自的 doc_id，不要合并成一个来源。
