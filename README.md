# 功能说明

这份代码的功能是

1. RooCode的OpenAI兼容模式，无法获取上下文使用量，原因是请求体中未携带include_usage参数。因此，需要开发一个产品，其开启一个请求转发服务，将请求转发，使其能走网络代理的同时，修改请求体，使其携带上usage信息，以此获取token使用量。

2. RooCode的Anthropic提供商模式，可以设置base_url，但是却无法设置模型的名字。在ZenMux供应商提供的模型中，其claude系列的模型名不是默认的，需要进行转换。因此，计划新增一个路由，支持Anthropic协议的转发，并将model name进行修改。（修改的方式为一个内置的全局字典，匹配到就转换，没匹配到就不转换。）

3. ZenMux提供商支持通过携带参数，让模型进行内置的网络搜索，该网络搜索相较于本地搜索更快，更无需本地进行更多的配置。因此，需要修改下Anthropic协议的请求体，添加网络搜索工具的json。参数的配置，同样是配置在脚本的顶层全局变量，方便用户修改。文档的地址为：https://zenmux.ai/docs/guide/advanced/web-search.html

## 使用方式

在 RooCode 中将你的自定义基础 URL 改为 `http://localhost:11732` （端口可修改源文件自行配置）