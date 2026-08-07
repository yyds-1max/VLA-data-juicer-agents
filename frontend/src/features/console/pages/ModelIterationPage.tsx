import { TrainingPlatform } from "../../training/TrainingPlatform";

type ModelIterationPageProps = { onPlaceholderAction?: (message?: string) => void };

/** The previous fixture-only model iteration view is now the training platform. */
export function ModelIterationPage(_props: ModelIterationPageProps) {
  return <TrainingPlatform />;
}
