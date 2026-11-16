export const Stat = ({ label, value, big }) => (
  <div className={big ? "stat big" : "stat"}><span className="muted">{label}</span><span>{value}</span></div>
);
