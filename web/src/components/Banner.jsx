export function Banner({ error, recover }) {
  if (!error) return null;
  return (
    <div className="error">
      <strong>{error.message}</strong>
      {error.hint && <p>{error.hint}</p>}
      {error.in_flight && <button onClick={recover}>Recover now</button>}
    </div>
  );
}
