import { useState, type FormEvent } from "react";
import { ArrowRight, Plus, Square } from "lucide-react";

type ComposerProps = {
  placeholder: string;
  running?: boolean;
  onSubmit: (message: string) => void;
  onInterrupt?: () => void;
};

export function Composer({ placeholder, running = false, onSubmit, onInterrupt }: ComposerProps) {
  const [message, setMessage] = useState("");

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();

    if (running) {
      onInterrupt?.();
      return;
    }

    const trimmed = message.trim();
    if (!trimmed) {
      return;
    }

    onSubmit(trimmed);
    setMessage("");
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="flex min-h-12 items-center gap-2 rounded border border-console-line bg-console-bg px-2 py-2"
    >
      <button
        type="button"
        aria-label="Add context"
        className="flex h-9 w-9 shrink-0 items-center justify-center rounded text-console-muted transition hover:bg-console-panel2 hover:text-console-text focus:outline-none focus:ring-2 focus:ring-console-cyan"
      >
        <Plus className="h-5 w-5" aria-hidden="true" />
      </button>
      <input
        value={message}
        onChange={(event) => setMessage(event.target.value)}
        placeholder={placeholder}
        className="min-w-0 flex-1 bg-transparent text-sm text-console-text outline-none placeholder:text-console-muted"
      />
      <button
        type="submit"
        aria-label={running ? "Stop current run" : "Send message"}
        className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-console-cyan text-console-bg transition hover:bg-cyan-200 focus:outline-none focus:ring-2 focus:ring-console-cyan focus:ring-offset-2 focus:ring-offset-console-bg"
      >
        {running ? (
          <Square className="h-3.5 w-3.5 fill-current" aria-hidden="true" />
        ) : (
          <ArrowRight className="h-5 w-5" aria-hidden="true" />
        )}
      </button>
    </form>
  );
}
