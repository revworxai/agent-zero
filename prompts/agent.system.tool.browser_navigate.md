### browser_navigate:
fast direct playwright - no LLM loop
PREFER over browser_agent for: navigation, JS extraction, screenshots
shares same browser window - ~2-3 sec vs 2+ min

#### actions
- goto: navigate URL
- evaluate: run JS on page
- screenshot: capture page
- get_info: current url/title/tab

#### usage
{"tool_name":"browser_navigate","tool_args":{"action":"goto","url":"https://app.revworx.ai/app2/serenity.pl#Contacts"}}
{"tool_name":"browser_navigate","tool_args":{"action":"evaluate","js":"document.title"}}
{"tool_name":"browser_navigate","tool_args":{"action":"screenshot"}}
{"tool_name":"browser_navigate","tool_args":{"action":"get_info"}}
