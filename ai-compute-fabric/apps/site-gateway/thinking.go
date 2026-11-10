package main

import (
	"encoding/json"
	"strings"
)

// ModelThinkingProfile inspects a model filename or ID and returns:
//   - supportsThinking: whether the model can produce <think> / reasoning_content
//   - supportsToggle:   whether thinking can be dynamically toggled on/off per request without reloading
//   - mode:             "hybrid" (toggleable per request), "always" (pure reasoning), or "none" (direct instruct/code)
func ModelThinkingProfile(model string) (supportsThinking, supportsToggle bool, mode string) {
	clean, aliasOverride := StripThinkingAlias(model)
	m := strings.ToLower(strings.TrimSpace(clean))
	if m == "" || strings.HasPrefix(m, "rpc://") || m == "rpc-server" {
		return false, false, "none"
	}
	if strings.Contains(m, "embed") || strings.Contains(m, "rerank") || strings.Contains(m, "bge") {
		return false, false, "none"
	}
	if aliasOverride != nil {
		if *aliasOverride {
			return true, true, "enabled"
		}
		return false, true, "disabled"
	}
	// Pure reasoning models (always think; can still be budget-capped via reasoning_budget=0)
	if strings.Contains(m, "deepseek-r1") || strings.Contains(m, "kimi-k3") || strings.Contains(m, "qwq") {
		return true, true, "always"
	}
	// Direct Instruct/Coder variants trained without <think> blocks
	if strings.Contains(m, "qwen3-coder") && strings.Contains(m, "instruct") {
		return false, false, "none"
	}
	if strings.Contains(m, "deepseek-coder") || strings.Contains(m, "deepseek-v2-lite") {
		return false, false, "none"
	}
	// Hybrid reasoning models (Qwen3 family, Qwen3.8, GLM-4.7-Flash) support instant /think & /no_think toggle
	if strings.Contains(m, "qwen3") || strings.Contains(m, "glm-4.7") || strings.Contains(m, "thinking") {
		return true, true, "hybrid"
	}
	return false, false, "none"
}

// ModelSupportsVision reports whether the model accepts image input. Mirrors the engine launcher
// (.aicf-work/bin/llama-server-vision), which attaches the vision projector only to Qwen3.8-27B.
func ModelSupportsVision(model string) bool {
	clean, _ := StripThinkingAlias(model)
	m := strings.ToLower(strings.TrimSpace(clean))
	if strings.Contains(m, "embed") || strings.Contains(m, "rerank") || strings.Contains(m, "bge") {
		return false
	}
	return strings.Contains(m, "qwen3.8")
}

// OllamaModelCapabilities returns the Ollama v0.9+ capabilities list for /api/show and /api/tags.
// Including "thinking" causes Open WebUI, Chatbox, Page Assist, AnythingLLM, Cline, and Ollama CLI
// to automatically render the Thinking toggle button.
func OllamaModelCapabilities(model string) []string {
	caps := []string{"completion", "tools"}
	if think, _, _ := ModelThinkingProfile(model); think {
		caps = append(caps, "thinking")
	}
	if ModelSupportsVision(model) {
		caps = append(caps, "vision")
	}
	return caps
}

// BuildOpenAIModelEntry returns a standard OpenAI /v1/models object enriched with OpenAI/OpenRouter/LiteLLM
// reasoning discovery metadata so 3rd-party tools detect thinking support automatically.
func BuildOpenAIModelEntry(model string) map[string]any {
	think, toggle, mode := ModelThinkingProfile(model)
	baseModel, aliasOverride := StripThinkingAlias(model)
	supportedParams := []string{
		"temperature",
		"top_p",
		"max_tokens",
		"max_completion_tokens",
		"stream",
		"stop",
		"tools",
		"tool_choice",
	}
	if think || toggle {
		supportedParams = append(supportedParams,
			"reasoning",
			"reasoning_effort",
			"include_reasoning",
			"thinking",
			"enable_thinking",
			"think",
			"chat_template_kwargs",
			"reasoning_budget",
		)
	}
	entry := map[string]any{
		"id":                   model,
		"object":               "model",
		"created":              int64(1735689600),
		"owned_by":             "aicf",
		"reasoning":            think,
		"thinking":             think,
		"thinking_toggle":      toggle,
		"thinking_mode":        mode,
		"vision":               ModelSupportsVision(model),
		"supported_parameters": supportedParams,
		"capabilities": map[string]any{
			"completion":      true,
			"chat":            true,
			"tools":           true,
			"reasoning":       think,
			"thinking":        think,
			"thinking_toggle": toggle,
			"vision":          ModelSupportsVision(model),
		},
	}
	if ModelSupportsVision(model) {
		// OpenRouter / LiteLLM-style discovery fields, next to capabilities.vision and "vision"
		entry["architecture"] = map[string]any{
			"modality":          "text+image->text",
			"input_modalities":  []string{"text", "image"},
			"output_modalities": []string{"text"},
		}
		entry["input_modalities"] = []string{"text", "image"}
		entry["supports_vision"] = true
	}
	if aliasOverride != nil {
		entry["is_alias"] = true
		entry["parent_model"] = baseModel
	}
	return entry
}

// BuildOpenAIModelEntries returns the base OpenAI /v1/models entry plus, for hybrid toggleable models,
// virtual ":no_think" and ":think" model entries so 3rd-party tools that only offer a model dropdown
// (e.g. Cursor, Continue.dev, Dify, n8n, LangChain) can also select Thinking ON vs OFF directly.
func BuildOpenAIModelEntries(model string) []map[string]any {
	entries := []map[string]any{BuildOpenAIModelEntry(model)}
	_, toggle, mode := ModelThinkingProfile(model)
	if toggle && mode == "hybrid" {
		entries = append(entries,
			BuildOpenAIModelEntry(model+":no_think"),
			BuildOpenAIModelEntry(model+":think"),
		)
	}
	return entries
}

// StripThinkingAlias checks if the requested model name carries a virtual suffix such as
// ":no_think", ":nothink", "-nothink", ":think", or "-think". It returns the base model name
// and the implied thinking toggle override (if any).
func StripThinkingAlias(model string) (string, *bool) {
	trimmed := strings.TrimSpace(model)
	low := strings.ToLower(trimmed)
	for _, suf := range []string{":no_think", ":nothink", "-no_think", "-nothink", "/no_think"} {
		if strings.HasSuffix(low, suf) {
			b := false
			return strings.TrimSpace(trimmed[:len(trimmed)-len(suf)]), &b
		}
	}
	for _, suf := range []string{":think", "-think", "/think"} {
		if strings.HasSuffix(low, suf) {
			b := true
			return strings.TrimSpace(trimmed[:len(trimmed)-len(suf)]), &b
		}
	}
	return trimmed, nil
}

func parseThinkValue(v any) *bool {
	switch val := v.(type) {
	case bool:
		b := val
		return &b
	case string:
		s := strings.ToLower(strings.TrimSpace(val))
		switch s {
		case "false", "0", "off", "none", "no", "no_think", "nothink", "disable", "disabled":
			b := false
			return &b
		case "true", "1", "on", "yes", "think", "minimal", "low", "medium", "high", "auto", "enable", "enabled":
			if s == "auto" {
				return nil
			}
			b := true
			return &b
		}
	case float64:
		b := val != 0
		return &b
	}
	return nil
}

// ExtractThinkingDirective inspects an arbitrary request map and optional X-AICF-Thinking header
// for any standard thinking control parameter across OpenAI, OpenRouter, LiteLLM, Anthropic-bridge,
// Ollama, Qwen/DashScope, vLLM, and llama.cpp.
func ExtractThinkingDirective(payload map[string]any, headerVal string) *bool {
	if p := parseThinkValue(headerVal); p != nil {
		return p
	}
	if payload == nil {
		return nil
	}
	// 1. chat_template_kwargs.enable_thinking (llama.cpp / vLLM / HuggingFace standard)
	if ctk, ok := payload["chat_template_kwargs"].(map[string]any); ok {
		if p := parseThinkValue(ctk["enable_thinking"]); p != nil {
			return p
		}
		if p := parseThinkValue(ctk["thinking"]); p != nil {
			return p
		}
	}
	// 2. extra_body wrapper (used by some OpenAI JS/Python clients)
	if eb, ok := payload["extra_body"].(map[string]any); ok {
		if ctk, ok := eb["chat_template_kwargs"].(map[string]any); ok {
			if p := parseThinkValue(ctk["enable_thinking"]); p != nil {
				return p
			}
		}
		if p := parseThinkValue(eb["enable_thinking"]); p != nil {
			return p
		}
		if p := parseThinkValue(eb["think"]); p != nil {
			return p
		}
	}
	// 3. Top-level enable_thinking (Qwen / DashScope / SGLang) or think (Ollama / Open WebUI)
	if p := parseThinkValue(payload["enable_thinking"]); p != nil {
		return p
	}
	if p := parseThinkValue(payload["think"]); p != nil {
		return p
	}
	// 4. Top-level include_reasoning (OpenRouter / LobeChat / Cherry Studio)
	if p := parseThinkValue(payload["include_reasoning"]); p != nil {
		return p
	}
	// 5. thinking object or bool (LiteLLM / Cherry Studio / Continue.dev / Anthropic-style {"type": "enabled"|"disabled"})
	if tObj, ok := payload["thinking"].(map[string]any); ok {
		if p := parseThinkValue(tObj["type"]); p != nil {
			return p
		}
		if p := parseThinkValue(tObj["enabled"]); p != nil {
			return p
		}
		if budget, ok := tObj["budget_tokens"].(float64); ok {
			b := budget > 0
			return &b
		}
	} else if p := parseThinkValue(payload["thinking"]); p != nil {
		return p
	}
	// 6. options.think (Ollama options object)
	if opts, ok := payload["options"].(map[string]any); ok {
		if p := parseThinkValue(opts["think"]); p != nil {
			return p
		}
		if p := parseThinkValue(opts["enable_thinking"]); p != nil {
			return p
		}
	}
	// 7. reasoning_effort (OpenAI o1/o3/o4-mini/GPT-5 standard: "none" disables, "minimal"/"low"/"medium"/"high" enables)
	if p := parseThinkValue(payload["reasoning_effort"]); p != nil {
		return p
	}
	// 8. reasoning object (OpenAI Responses API & OpenRouter: {"effort": "none"}, {"enabled": false}, {"exclude": true}, {"max_tokens": 0})
	if rObj, ok := payload["reasoning"].(map[string]any); ok {
		if p := parseThinkValue(rObj["enabled"]); p != nil {
			return p
		}
		if p := parseThinkValue(rObj["effort"]); p != nil {
			return p
		}
		if ex, ok := rObj["exclude"].(bool); ok {
			b := !ex
			return &b
		}
		if mt, ok := rObj["max_tokens"].(float64); ok {
			b := mt > 0
			return &b
		}
	}
	// 9. reasoning_budget (llama.cpp native: 0 disables thinking, >0 or -1 enables)
	if rb, ok := payload["reasoning_budget"].(float64); ok {
		b := rb != 0
		return &b
	}
	// 10. Model name suffix (:no_think / :think)
	if m, ok := payload["model"].(string); ok {
		if _, override := StripThinkingAlias(m); override != nil {
			return override
		}
	}
	return nil
}

// ApplyThinkingControl normalizes any client thinking toggle in a JSON chat/completion payload
// into llama-server's native chat_template_kwargs + soft-switch prompt tag (/no_think or /think)
// without reloading the model.
func ApplyThinkingControl(body []byte, headerVal string) (rewritten []byte, cleanModel string, thinkState *bool) {
	var payload map[string]any
	if json.Unmarshal(body, &payload) != nil || payload == nil {
		return body, "", nil
	}
	rawModel, _ := payload["model"].(string)
	cleanModel, aliasOverride := StripThinkingAlias(rawModel)
	thinkState = ExtractThinkingDirective(payload, headerVal)
	if thinkState == nil {
		thinkState = aliasOverride
	}
	if thinkState == nil && cleanModel == rawModel {
		return body, cleanModel, nil
	}
	if cleanModel != rawModel {
		payload["model"] = cleanModel
	}
	if thinkState != nil {
		en := *thinkState
		ctk, _ := payload["chat_template_kwargs"].(map[string]any)
		if ctk == nil {
			ctk = map[string]any{}
		}
		ctk["enable_thinking"] = en
		payload["chat_template_kwargs"] = ctk
		payload["enable_thinking"] = en
		if !en {
			payload["reasoning_budget"] = 0
		}
		// Inject Qwen3 soft-switch token (/no_think or /think) into the last user message
		// so GGUF templates that check message text obey the toggle immediately.
		injectSoftSwitchTag(payload, en)
	}
	out, err := json.Marshal(payload)
	if err != nil {
		return body, cleanModel, thinkState
	}
	return out, cleanModel, thinkState
}

func injectSoftSwitchTag(payload map[string]any, enable bool) {
	msgs, ok := payload["messages"].([]any)
	if !ok || len(msgs) == 0 {
		return
	}
	tag := "/no_think"
	if enable {
		tag = "/think"
	}
	for i := len(msgs) - 1; i >= 0; i-- {
		m, ok := msgs[i].(map[string]any)
		if !ok {
			continue
		}
		role, _ := m["role"].(string)
		if role != "user" {
			continue
		}
		content, ok := m["content"].(string)
		if !ok {
			return
		}
		trimmed := strings.TrimSpace(content)
		if strings.HasSuffix(trimmed, "/no_think") || strings.HasSuffix(trimmed, "/think") {
			return
		}
		m["content"] = content + " " + tag
		return
	}
}

// CleanDisabledThinkingContent strips <think>...</think> blocks from output when thinking is disabled.
func CleanDisabledThinkingContent(s string) string {
	t := strings.TrimSpace(s)
	if strings.HasPrefix(t, "<think>") {
		if idx := strings.Index(t, "</think>"); idx >= 0 {
			return strings.TrimLeft(t[idx+len("</think>"):], "\r\n ")
		}
	}
	return s
}

// NormalizeOpenAIChatResponse ensures that non-streaming OpenAI /v1/chat/completions responses
// strictly match client expectations when thinking is toggled off (stripping <think> and reasoning_content)
// or toggled on (populating reasoning_content if <think> was embedded in content).
func NormalizeOpenAIChatResponse(respBody []byte, thinkState *bool) []byte {
	var root map[string]any
	if json.Unmarshal(respBody, &root) != nil || root == nil {
		return respBody
	}
	choices, ok := root["choices"].([]any)
	if !ok || len(choices) == 0 {
		return respBody
	}
	modified := false
	for _, c := range choices {
		ch, ok := c.(map[string]any)
		if !ok {
			continue
		}
		msg, ok := ch["message"].(map[string]any)
		if !ok {
			continue
		}
		content, _ := msg["content"].(string)
		if thinkState != nil && !*thinkState {
			cleaned := CleanDisabledThinkingContent(content)
			if cleaned != content {
				msg["content"] = cleaned
				modified = true
			}
			if _, hasRC := msg["reasoning_content"]; hasRC {
				delete(msg, "reasoning_content")
				modified = true
			}
			if _, hasR := msg["reasoning"]; hasR {
				delete(msg, "reasoning")
				modified = true
			}
		} else {
			// If <think>...</think> is embedded in content without reasoning_content, extract it
			// for OpenAI/DeepSeek/OpenRouter clients while preserving clean content.
			rc, _ := msg["reasoning_content"].(string)
			if rc == "" && strings.HasPrefix(strings.TrimSpace(content), "<think>") {
				t := strings.TrimSpace(content)
				if idx := strings.Index(t, "</think>"); idx >= 0 {
					thought := strings.TrimSpace(t[len("<think>"):idx])
					rest := strings.TrimLeft(t[idx+len("</think>"):], "\r\n ")
					if thought != "" {
						msg["reasoning_content"] = thought
						msg["content"] = rest
						modified = true
					}
				}
			}
		}
	}
	if !modified {
		return respBody
	}
	out, err := json.Marshal(root)
	if err != nil {
		return respBody
	}
	return out
}
