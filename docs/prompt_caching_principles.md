# Prompt Caching Principles

## What It Is
Reuse repeated context across API requests instead of paying full price every turn. Cached tokens cost ~10% of normal input tokens.

## How It Works
- LLM inference has a **prefill phase** (processing the prompt) and a **decode phase** (generating output)
- Caching stores the prefill computation so identical content doesn't need to be reprocessed
- A cryptographic hash of all content up to a breakpoint is created — **one character difference = cache miss**

## Usage
Place a `cache_control` breakpoint where you want caching to apply:
```json
{
  "cache_control": {"type": "ephemeral"},
  "messages": [...]
}
```
With **auto-caching**, the breakpoint automatically moves to the last cacheable block as the conversation grows.

## Prompt Design for Maximum Cache Hits
1. **Stable content first** — system prompt, tool definitions, instructions
2. **Dynamic content last** — new user messages, latest observations
3. **Never edit history** — modifying past turns breaks the hash
4. **Cache hit rate** is the most important production metric for agents

## When It Matters Most
- Long-running agents that act in a loop
- Any app that sends the same context (instructions, tools, history) repeatedly
- Token-heavy workflows like coding agents — without caching, costs become prohibitive
