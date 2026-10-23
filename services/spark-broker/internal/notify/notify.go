// Package notify posílá zprávy na Telegram přes existující notify.sh
// (PromoClown bot; token zůstává v jeho env souboru na SPARKu, broker ho nevidí).
package notify

import (
	"context"
	"log/slog"
	"os/exec"
	"time"
)

type Cmd struct{ Path string }

func (c Cmd) Send(text string) {
	if c.Path == "" {
		slog.Info("notify (vypnuto)", "text", text)
		return
	}
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		if out, err := exec.CommandContext(ctx, c.Path, "<b>broker</b> "+text).CombinedOutput(); err != nil {
			slog.Warn("notify selhal", "err", err, "out", string(out))
		}
	}()
}
