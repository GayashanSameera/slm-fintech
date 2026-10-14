// Entry point: mount the console into <div id="root"> (index.html).
// StrictMode runs extra checks in development only (e.g. effects twice); no effect in the build.
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
