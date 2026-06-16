package compose

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
)

// Runner controls docker compose stacks.
type Runner interface {
	Up(ctx context.Context, composeFile, project string, services []string) error
	Down(ctx context.Context, composeFile, project string, services []string) error
}

// DockerRunner invokes the docker compose CLI.
type DockerRunner struct{}

func New() *DockerRunner { return &DockerRunner{} }

// Up starts the compose stack. If services is non-empty, only those services are started.
// project is passed as -p to isolate stacks that share a compose directory.
func (r *DockerRunner) Up(ctx context.Context, composeFile, project string, services []string) error {
	args := append([]string{"up", "-d", "--remove-orphans"}, services...)
	return run(ctx, composeFile, project, args...)
}

// Down stops the compose stack. If services is non-empty, only those services are stopped and
// removed; otherwise the entire stack is brought down.
func (r *DockerRunner) Down(ctx context.Context, composeFile, project string, services []string) error {
	if len(services) == 0 {
		return run(ctx, composeFile, project, "down")
	}
	if err := run(ctx, composeFile, project, append([]string{"stop"}, services...)...); err != nil {
		return err
	}
	return run(ctx, composeFile, project, append([]string{"rm", "-f"}, services...)...)
}

func run(ctx context.Context, composeFile, project string, args ...string) error {
	cmdArgs := []string{"compose", "-f", composeFile}
	if project != "" {
		cmdArgs = append(cmdArgs, "-p", project)
	}
	cmdArgs = append(cmdArgs, args...)
	cmd := exec.CommandContext(ctx, "docker", cmdArgs...)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("docker %v: %w: %s", cmdArgs, err, stderr.String())
	}
	return nil
}
