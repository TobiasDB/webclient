import type { Preview } from "@storybook/react-vite";
import "../src/styles.css";

const preview: Preview = {
  parameters: {
    layout: "padded",
    backgrounds: { disable: true },
    options: {
      storySort: {
        order: ["Foundations", "Primitives", "Domain", "Wireframes", "Scenes"],
      },
    },
  },
  globalTypes: {
    theme: {
      description: "Colour scheme",
      toolbar: { icon: "circlehollow", items: ["light", "dark"], dynamicTitle: true },
    },
  },
  initialGlobals: { theme: "light" },
  decorators: [
    (Story, ctx) => {
      document.documentElement.dataset.theme = ctx.globals.theme ?? "light";
      return Story();
    },
  ],
};
export default preview;
