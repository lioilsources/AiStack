package state

import (
	"path/filepath"
	"testing"
	"time"
)

func TestReadWriteRoundtrip(t *testing.T) {
	dir := t.TempDir()
	s := New(filepath.Join(dir, "state.json"))

	// Empty file returns zero State.
	got, err := s.Read()
	if err != nil {
		t.Fatal(err)
	}
	if got.Active != "" {
		t.Fatalf("expected empty active, got %q", got.Active)
	}

	want := State{Active: "lab", Healthy: true, Since: time.Now().Truncate(time.Second)}
	if err := s.Write(want); err != nil {
		t.Fatal(err)
	}
	got, err = s.Read()
	if err != nil {
		t.Fatal(err)
	}
	if got.Active != want.Active {
		t.Fatalf("active: want %q, got %q", want.Active, got.Active)
	}
	if got.Healthy != want.Healthy {
		t.Fatalf("healthy: want %v, got %v", want.Healthy, got.Healthy)
	}
}

func TestWriteCreatesDir(t *testing.T) {
	dir := t.TempDir()
	s := New(filepath.Join(dir, "nested", "dir", "state.json"))
	if err := s.Write(State{Active: "lab"}); err != nil {
		t.Fatal(err)
	}
}
