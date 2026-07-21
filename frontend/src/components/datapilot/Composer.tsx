import {
  forwardRef,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
  type ForwardedRef,
  type KeyboardEvent,
} from "react";
import { ArrowUp, LoaderCircle, Paperclip, Square } from "lucide-react";

type ComposerProps = {
  placeholder: string;
  running?: boolean;
  interrupting?: boolean;
  onSubmit: (message: string) => void;
  onInterrupt?: () => void;
};

const MAX_TEXTAREA_HEIGHT = 132;

export const Composer = forwardRef<HTMLTextAreaElement, ComposerProps>(function Composer(
  { placeholder, running = false, interrupting = false, onSubmit, onInterrupt },
  ref,
) {
  const [message, setMessage] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  useLayoutEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    const nextHeight = Math.min(textarea.scrollHeight, MAX_TEXTAREA_HEIGHT);
    textarea.style.height = `${nextHeight}px`;
    textarea.style.overflowY = textarea.scrollHeight > MAX_TEXTAREA_HEIGHT ? "auto" : "hidden";
  }, [message]);

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

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    event.currentTarget.form?.requestSubmit();
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="flex min-h-14 items-end gap-2 rounded-[18px] border border-console-line/90 bg-console-panel/95 px-2.5 py-2.5 shadow-[0_2px_10px_rgba(23,32,46,0.06)] backdrop-blur-sm transition-[border-color,box-shadow] duration-150 focus-within:border-console-cyan/45 focus-within:shadow-[0_3px_12px_rgba(23,32,46,0.075)] motion-reduce:transition-none"
    >
      <span
        className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl text-console-muted"
        aria-hidden="true"
      >
        <Paperclip className="h-4 w-4" aria-hidden="true" />
      </span>
      <textarea
        ref={(node) => assignRef(textareaRef, ref, node)}
        rows={1}
        value={message}
        onChange={(event) => setMessage(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        className="console-soft-scrollbar my-1 min-h-6 min-w-0 flex-1 resize-none bg-transparent text-sm leading-6 text-console-text outline-none transition-[height] duration-100 placeholder:text-console-muted motion-reduce:transition-none"
      />
      <button
        type="submit"
        aria-label={interrupting ? "Interrupt requested" : running ? "Stop current run" : "Send message"}
        disabled={interrupting}
        className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-console-text text-white transition motion-reduce:transition-none hover:bg-slate-800 focus:outline-none focus:ring-2 focus:ring-console-cyan focus:ring-offset-2 focus:ring-offset-console-bg"
      >
        {interrupting ? (
          <LoaderCircle className="h-5 w-5 motion-safe:animate-spin" aria-hidden="true" />
        ) : running ? (
          <Square className="h-3.5 w-3.5 fill-current" aria-hidden="true" />
        ) : (
          <ArrowUp className="h-5 w-5" aria-hidden="true" />
        )}
      </button>
    </form>
  );
});

function assignRef(
  localRef: { current: HTMLTextAreaElement | null },
  forwardedRef: ForwardedRef<HTMLTextAreaElement>,
  node: HTMLTextAreaElement | null,
) {
  localRef.current = node;
  if (typeof forwardedRef === "function") forwardedRef(node);
  else if (forwardedRef) forwardedRef.current = node;
}
