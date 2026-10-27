package catalog

import (
	"os"
	"testing"
)

// Vzorová konfigurace z repa musí jít načíst a pokrýt typické úlohy.
func TestExampleConfig(t *testing.T) {
	b, err := os.ReadFile("../../deploy/config.example.yaml")
	if err != nil {
		t.Fatal(err)
	}
	c, err := Parse(b)
	if err != nil {
		t.Fatal(err)
	}
	for svcs, want := range map[string]string{
		"comfyui":      "comfy",
		"qwen36":       "llm",
		"director":     "director",
		"gptoss":       "gptoss",
		"audio-music":  "comfy",
		"flux-schnell": "llm", // od 2026-10-27 v comfy není
	} {
		if got, ok := c.Target("", []string{svcs}); !ok || got != want {
			t.Errorf("%s → %q, čekal %q", svcs, got, want)
		}
	}
	if got, _ := c.Target("llm", []string{"flux-schnell"}); got != "llm" {
		t.Errorf("flux v llm se nemá přepínat, dostal %s", got)
	}
	if got, _ := c.Target("comfy", []string{"comfyui"}); got != "comfy" {
		t.Errorf("comfyui v comfy se nemá přepínat, dostal %s", got)
	}
	if _, ok := c.Target("", []string{"comfyui", "qwen36"}); ok {
		t.Error("ComfyUI s qwen36 nesmí jít dohromady")
	}
	if _, ok := c.Target("", []string{"director", "flux-schnell"}); ok {
		t.Error("director s fluxem nesmí jít dohromady (25. 9. pád stroje)")
	}
	if c.ProjectMax("neznamy") != 20 || c.ProjectMax("storyteller") != 50 {
		t.Error("stropy projektů")
	}
}
