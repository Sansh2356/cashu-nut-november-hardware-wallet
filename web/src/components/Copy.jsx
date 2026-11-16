import { useState } from "react";

export function Copy({ text }) {
  const [done, setDone] = useState(false);
  return (
    <button className="ghost" onClick={() => navigator.clipboard.writeText(text).then(() => setDone(true))}>
      {done ? "Copied" : "Copy"}
    </button>
  );
}
