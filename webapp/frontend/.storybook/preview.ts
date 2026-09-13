import type { Preview } from "@storybook/react";
import "../src/index.css";

const preview: Preview = {
  parameters: {
    layout: "padded",
    backgrounds: {
      default: "paper",
      values: [
        { name: "paper", value: "#f7f7f5" },
        { name: "dark", value: "#141619" },
      ],
    },
    controls: {
      matchers: {
        color: /(background|color)$/i,
        date: /Date$/i,
      },
    },
  },
};

export default preview;
