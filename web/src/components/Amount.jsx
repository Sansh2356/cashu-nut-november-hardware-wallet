export const Amount = ({ value, set }) => (
  <label>Amount (sat) <input type="number" min="1" required value={value} onChange={(e) => set(e.target.value)} /></label>
);
