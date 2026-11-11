"""Textual 主题与布局样式；ChatApp 通过 CSS = APP_CSS 应用。"""

from __future__ import annotations

from rich.theme import Theme

# Scoped to reply rendering; other Rich/CLI output keeps its own theme.
MARKDOWN_THEME = Theme(
    {
        "markdown.text": "#d5d5d5",
        "markdown.strong": "bold #f0f0f0",
        "markdown.emph": "italic #b8c7d9",
        "markdown.h1": "bold #6cb6ff not underline",
        "markdown.h2": "bold #91c9ff not underline",
        "markdown.h3": "bold #bbd9f4 not underline",
        "markdown.h4": "bold #c5c5c5 not underline",
        "markdown.h5": "bold #c5c5c5 not underline",
        "markdown.h6": "bold #c5c5c5 not underline",
        "markdown.link": "#6cb6ff underline",
        "markdown.link_url": "#6cb6ff underline",
        "markdown.code": "#e2c08d on #282828",
        "markdown.block_quote": "italic #9aafc4",
        "markdown.table.border": "#444444",
        "markdown.table.header": "bold #bbd9f4",
        "markdown.item.bullet": "#6cb6ff",
        "markdown.item.number": "#6cb6ff",
        "markdown.hr": "#444444",
    }
)

APP_CSS = """
    /* Evolution: neutral surfaces, a clear sky-blue accent, and content-led hierarchy. */
    $accent: #6cb6ff;
    $secondary: #a6a6a6;
    $warm: #a6a6a6;
    $dim: #3a3a3a;
    $muted: #a6a6a6;
    $faint: #818181;
    $panel: #1d1d1d;
    $bg: #171717;
    $scroll-track: #171717;
    $scroll-thumb: #484848;
    $scroll-thumb-hover: #818181;
    $scroll-thumb-active: #999999;

    Screen { background: $bg; }
    /* 统一窄滚动条；新的可滚动组件加 thin-scroll 类即可复用。 */
    .thin-scroll {
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
        scrollbar-background: $scroll-track;
        scrollbar-color: $scroll-thumb;
        scrollbar-color-hover: $scroll-thumb-hover;
        scrollbar-color-active: $scroll-thumb-active;
    }
    #root { width: 1fr; height: 1fr; }
    #sidebar { width: 28; background: #121212; border-right: solid #292929; }
    #sidebar-head { height: 3; align: left middle; }
    #brand { color: #cccccc; text-style: bold; padding: 0 1; width: 1fr; }
    /* 折叠按钮只留符号，减少侧栏头部的方框噪音 */
    #hide-sidebar { width: 3; height: 3; min-width: 3; padding: 0; background: #121212; color: $faint; border: none; margin-right: 1; content-align: center middle; }
    #hide-sidebar:hover { color: $accent; }
    #show-sidebar { layer: overlay; dock: top; width: 3; height: 3; min-width: 3; padding: 0; background: $bg; color: $faint; border: none; content-align: center middle; display: none; }
    #show-sidebar:hover { color: $accent; }
    #new-session { width: 1fr; height: 3; margin: 0 1 1 1; background: #1c1c1c; color: #bdbdbd; border: none; content-align: left middle; padding: 0 1; }
    #new-session:hover { background: #292929; color: $accent; }
    /* 列表与 New session 用同一左右外边距；隐藏滚动条，避免出现滚动条时行宽缩 2 格错位 */
    #sessions { background: transparent; width: 1fr; height: 1fr; margin: 0 1; scrollbar-size-vertical: 0; }
    /* ListItem 默认高度跟随子容器（Vertical 是 1fr 会撑满列表），必须钉死 auto */
    #sessions > ListItem { background: transparent; padding: 0 1; height: auto; }
    #sessions > ListItem > Vertical { height: auto; }
    .session-title { color: #d4d4d4; width: 1fr; overflow-x: hidden; text-overflow: ellipsis; }
    .session-meta { color: #8e8e8e; }
    #session-actions { height: 1; margin-top: 1; }
    #session-actions Button { height: 1; min-width: 0; border: none; background: #242424; color: #d4d4d4; padding: 0 2; }
    #session-actions Button:hover, #session-actions Button:focus { background: #303030; color: $accent; }
    /* 当前会话保留左线作为非颜色标识，低饱和底色区分焦点。 */
    #sessions > ListItem.-highlight { background: #272727; }
    #sessions > ListItem.current { background: #272727; border-left: heavy $accent; }
    #sessions > ListItem.current.-highlight { background: #303030; border-left: heavy $accent; }
    .group-label { color: #8e8e8e; padding: 1 1 0 1; }

    #main { width: 1fr; layers: base overlay; }
    #chat { width: 100%; height: 1fr; padding: 1 0 1 3; }
    /* 右侧留白改由子元素承担，滚动条才能贴在窗口最右侧 */
    #chat > * { margin-right: 2; }
    /* 长回复用固定上限的阅读列，窄窗口继续使用全部可用宽度。 */
    #chat > .user-turn, #chat > .who-ai, #chat > .reply, #chat > .meta, #chat > .thinking-panel { max-width: 100; }
    #chat > .who-ai { margin: 1 2 1 0; }
    #chat > .meta { margin: 1 2 0 0; }
    .who-user { content-align: left top; color: $faint; width: 1fr; margin-top: 1; }
    .user-turn { width: 1fr; height: auto; align: left top; margin-bottom: 1; }
    .bubble { background: #222222; color: #dedede; border-left: solid $accent; padding: 1 2; width: 1fr; height: auto; }
    .who-ai { color: #b5cfe7; text-style: bold; width: 1fr; }
    .reply { color: #d5d5d5; width: 1fr; height: auto; }
    .thinking-panel { width: 1fr; height: auto; margin-top: 1; }
    .thinking-toggle { height: 1; min-width: 0; border: none; padding: 0 1; color: $muted; background: $panel; }
    .thinking-toggle:hover, .thinking-toggle:focus { color: $accent; }
    .thinking-text { height: 3; border: none; padding: 0 1; color: $muted; background: $panel; }
    .thinking-text:focus { border: none; }
    /* 工具事件用薄边线和弱底色分组，不与正式回复争夺注意力 */
    .event-card { border-left: solid $dim; background: transparent; color: $muted; padding: 0 1; width: 1fr; }
    .event-card.memo { border-left: solid $accent; color: #b9b9b9; }
    .meta { color: $faint; width: 1fr; }
    .divider { height: 1; margin: 0; }
    .card { border-left: solid $dim; background: $panel; padding: 0 2; margin: 1 0; color: #d4d4d4; width: 1fr; }
    /* 欢迎区只保留完整项目名字标，不用数据卡片填满留白。 */
    #chat.home { align: center middle; }
    .welcome-shell { width: 78; max-width: 100%; height: auto; padding: 1 0; align: center top; }
    .welcome-wordmark { width: 44; height: 7; margin-bottom: 1; }
    .welcome-line { color: #929292; width: 1fr; content-align: center top; height: auto; }

    /* 输入框不做焦点整块提亮，避免像额外遮罩；只用边框区分焦点。 */
    #send-queue { display: none; height: auto; max-height: 5; margin: 0 3; padding: 0 1; color: $muted; border-left: solid $dim; }
    #input { height: 3; margin: 0 3; padding: 0 2; background: #202020; background-tint: transparent; border: none; border-top: solid #202020; border-bottom: solid #202020; border-left: heavy #555555; border-title-color: #999999; border-title-align: left; }
    #input:focus { border-left: heavy $accent; }
    /* 光标由 BarInput 渲染成竖线；这里只提供前景色，避免默认整格反色块。 */
    #input > .input--cursor {
        background: transparent;
        color: #dedede;
    }
    /* 模型与上下文保持单行，快捷键独立一行，避免小窗口互相挤压。 */
    #status-row { height: 2; width: 1fr; background: $bg; padding: 1 3 0 3; }
    #composer-hint { height: 1; color: #858585; padding: 0 3; }
    #model-status { width: 1fr; height: 1; color: $muted; overflow-x: hidden; }
    #status { width: auto; max-width: 55%; height: 1; color: $faint; text-align: right; }


    /* 命令面板：浮在输入框上方，只显示命令、参数和一句短语。
       -6 = status 2 行 + 快捷键 1 行 + 输入框 3 行；偏移不够会盖住输入框。 */
    #cmd-palette { layer: overlay; dock: bottom; offset-x: 3; offset-y: -6; width: 64; max-width: 92%; height: auto; max-height: 16; background: $panel; border: round #484848; padding: 0 1; display: none; }
    #cmd-list { height: auto; max-height: 14; scrollbar-size-vertical: 1; }
    .cmd-row { width: 1fr; height: 1; padding: 0 1; color: #d1d1d1; }
    .cmd-row.cmd-active { background: #303030; }
    .cmd-usage { color: $accent; }
    .cmd-desc { color: #a3a3a3; }

    PickScreen, SessionPickerScreen, TextInputScreen, ProfileScreen { align: center middle; background: #0d0d0d 75%; }
    #profile-card { width: 78; max-width: 92%; height: auto; max-height: 85%; background: $panel; border: round #484848; padding: 1 2; }
    #profile-title { color: $accent; text-style: bold; }
    #profile-subtitle { color: $faint; margin-bottom: 1; }
    #profile-card.long { height: 85%; }
    #profile-body { height: auto; max-height: 55vh; min-height: 3; margin-top: 1; padding: 1 2; background: $bg; }
    #profile-card.long #profile-body { height: 1fr; }
    #profile-text { height: auto; width: 1fr; color: #d5d5d5; }
    .profile-status { height: auto; color: $accent; }
    .profile-status.stale { color: #d1a05a; }
    #profile-meta { height: auto; color: #858585; }
    #profile-hint { height: auto; color: $warm; margin-top: 1; }
    GitScreen, GitConfirmScreen { align: center middle; background: #0d0d0d 75%; }
    #git-card { width: 72; max-width: 92%; height: auto; max-height: 85%; background: $panel; border: round #484848; padding: 1 2; }
    #git-body { height: auto; max-height: 55vh; min-height: 8; padding: 0 1; background: $bg; }
    #git-content { height: auto; width: 1fr; color: #d5d5d5; }
    #git-action { height: 1; color: $accent; margin-top: 1; }
    #git-confirm { height: auto; color: #d5d5d5; }
    GitConfirmScreen #git-card { width: 68; height: auto; max-height: 85%; }
    #git-confirm-body { height: auto; max-height: 35vh; min-height: 3; }
    #git-confirm-actions { height: 1; margin-top: 1; }
    #git-confirm-actions Button { height: 1; min-width: 0; border: none; background: #242424; color: #d4d4d4; margin-right: 1; padding: 0 2; }
    #git-confirm-actions Button:hover, #git-confirm-actions Button:focus { background: #303030; color: $accent; }
    #pick-card { width: 64; height: auto; max-height: 70%; background: $panel; border: round #484848; padding: 1 2; }
    SessionPickerScreen #pick-card { max-width: 92%; max-height: 85%; }
    #picker-input, #pick-search, #pick-input { width: 1fr; background: #171717; color: #dedede; border: round #414141; }
    #picker-input:focus, #pick-search:focus, #pick-input:focus { background: #222222; border: round #6cb6ff; }
    #picker-input, #pick-search { margin-bottom: 1; }
    #picker-list { height: auto; max-height: 16; }
    /* 竖线只标记选中行：普通行保留透明占位，当前行用底色占位，选中行只换颜色；
       三者盒模型一致，位置、宽度和高度不变。内层 Vertical 默认 1fr 会撑满列表，必须钉死 auto。 */
    #picker-list > ListItem { padding: 0 1; height: auto; border-left: heavy transparent; }
    #picker-list > ListItem > Vertical { height: auto; }
    #picker-list > ListItem.current { background: #272727; border-left: heavy #272727; }
    #picker-list > ListItem.-highlight { background: #303030; border-left: heavy $accent; }
    #picker-list > ListItem.current.-highlight { background: #303030; border-left: heavy $accent; }
    .pick-title { color: $accent; text-style: bold; margin-bottom: 1; }
    #pick-list { height: auto; max-height: 18; }
    #pick-list > ListItem { height: auto; }
    #pick-list > ListItem.-highlight { background: #303030; }
    /* 模型列表独立滚动，为小窗口的搜索框和快捷键提示保留空间。 */
    ModelPickerScreen #pick-card { height: 80%; max-height: 90%; max-width: 95%; }
    ModelPickerScreen #pick-list { height: 1fr; min-height: 1; }
    /* Full pending text scrolls independently; decision actions stay visible. */
    #pick-card.has-detail { width: 76; max-width: 90%; height: auto; max-height: 85%; }
    #pick-detail { height: auto; max-height: 25vh; min-height: 1; margin-bottom: 1; padding: 0 1; background: $bg; }
    #pick-detail-text { height: auto; width: 1fr; color: #d5d5d5; }
    #pick-card.has-detail #pick-list { height: auto; max-height: 6; min-height: 3; }
    .pick-hint { color: $warm; margin-top: 1; }

    /* 记忆审查：作用域独立，列表与详情在同一卡片内，不影响其他选择器。 */
    InferenceScreen, PendingScreen { align: center middle; background: #0d0d0d 75%; }
    #inference-card, #pending-card { width: 96; max-width: 94%; height: 85%; max-height: 90%; background: $panel; border: round #484848; padding: 1 2; }
    #inference-title, #pending-title { color: $accent; text-style: bold; }
    #inference-subtitle, #pending-subtitle { color: $faint; margin-bottom: 1; }
    #inference-filters, #pending-count { height: auto; margin-bottom: 1; color: #8e8e8e; }
    #inference-empty { color: $muted; height: auto; padding: 1 0; display: none; }
    #inference-body, #pending-body { height: 1fr; }
    /* OptionList 只保留文本选项，数百条推断时挂载成本远低于每项多控件 ListView。 */
    #inference-list, #pending-list { width: 38%; height: 1fr; background: #191919; border: none; border-right: solid #2a2a2a; padding: 0 1; }
    #pending-list { text-wrap: nowrap; text-overflow: ellipsis; }
    #inference-list > .option-list--option, #pending-list > .option-list--option { padding: 0 1; }
    #inference-list > .option-list--option-highlighted, #pending-list > .option-list--option-highlighted { background: #303030; }
    #inference-detail, #pending-detail { width: 1fr; height: 1fr; padding-left: 2; }
    #inference-detail-body, #pending-detail-body { height: 1fr; }
    #inference-detail-status, #pending-topic { color: $accent; height: auto; margin-bottom: 1; }
    .inference-detail-line { color: #8e8e8e; height: auto; margin-bottom: 1; }
    #inference-detail-text, #pending-text { color: #dedede; height: auto; margin-bottom: 1; }
    #inference-detail-sources { color: #9a9a9a; height: auto; }
    #inference-editor, #pending-editor { height: 1fr; display: none; background: #171717; border: round #414141; }
    #inference-actions, #inference-edit-actions, #pending-actions, #pending-edit-actions { height: 1; margin-top: 1; }
    #inference-actions Button, #inference-edit-actions Button, #pending-actions Button, #pending-edit-actions Button { height: 1; min-width: 0; border: none; background: #242424; color: #d4d4d4; margin-right: 1; padding: 0 2; }
    #inference-actions Button:hover, #inference-edit-actions Button:hover, #pending-actions Button:hover, #pending-edit-actions Button:hover { background: #303030; color: $accent; }
    #inference-actions Button:disabled, #inference-edit-actions Button:disabled, #pending-actions Button:disabled, #pending-edit-actions Button:disabled { color: #5f5f5f; background: #1c1c1c; }
    #inference-confirm, #pending-approve { color: $accent; }
    #inference-message, #pending-message { height: auto; color: $muted; margin-top: 1; }
    #inference-message.error, #pending-message.error { color: #d1a05a; }
    #inference-hint, #pending-hint { color: $warm; height: auto; margin-top: 1; }
    """
