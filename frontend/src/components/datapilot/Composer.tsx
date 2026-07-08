import { useState, type FormEvent } from "react";
import { ArrowUp, LoaderCircle, Paperclip, Square } from "lucide-react";

type ComposerProps = {
  placeholder: string;
  running?: boolean;
  interrupting?: boolean;
  onSubmit: (message: string) => void;
  onInterrupt?: () => void;
};

export function Composer({ placeholder, running = false, interrupting = false, onSubmit, onInterrupt }: ComposerProps) {
  const [message, setMessage] = useState("");

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();

    if (running) {
      if (interrupting) {
        return;
      }
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
      className="flex min-h-12 items-center gap-2 rounded-lg border border-console-line bg-console-panel px-2 py-2 shadow-sm"
    >
      <span
        className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-console-muted"
        aria-hidden="true"
      >
        <Paperclip className="h-4 w-4" aria-hidden="true" />
      </span>
      <input
        value={message}
        onChange={(event) => setMessage(event.target.value)}
        placeholder={placeholder}
        className="min-w-0 flex-1 bg-transparent text-sm text-console-text outline-none placeholder:text-console-muted"
      />
      <button
        type="submit"
        aria-label={interrupting ? "Interrupt requested" : running ? "Stop current run" : "Send message"}
        disabled={interrupting}
        className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-console-text text-white transition hover:bg-slate-800 focus:outline-none focus:ring-2 focus:ring-console-cyan focus:ring-offset-2 focus:ring-offset-console-bg"
      >
        {interrupting ? (
          <LoaderCircle className="h-5 w-5 animate-spin" aria-hidden="true" />
        ) : running ? (
          <Square className="h-3.5 w-3.5 fill-current" aria-hidden="true" />
        ) : (
          <ArrowUp className="h-5 w-5" aria-hidden="true" />
        )}
      </button>
    </form>
  );
}
