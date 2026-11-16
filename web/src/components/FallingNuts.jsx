import { useEffect, useRef } from "react";
import gsap from "gsap";

const COUNT = 14;

// Background decoration: cashews drifting down behind the cards. Each nut owns one column
// (so they never crowd horizontally) and starts a staggered moment later (so they keep a
// vertical gap). Off entirely for people who ask the system for reduced motion.
export function FallingNuts() {
  const root = useRef(null);
  useEffect(() => {
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    const ctx = gsap.context(() => {
      gsap.utils.toArray(".nut").forEach((el, i) => {
        // ponytail: fall distance is read once; a resize leaves a short or long fall until reload
        gsap.set(el, { left: `${(i + gsap.utils.random(0.2, 0.8)) * (100 / COUNT)}%`, y: -60,
          scale: gsap.utils.random(0.6, 1.1), rotation: gsap.utils.random(-30, 30) });
        gsap.to(el, { y: window.innerHeight + 60, rotation: `+=${gsap.utils.random(-200, 200)}`,
          duration: gsap.utils.random(14, 20), ease: "none", repeat: -1 })
          .progress(((i * 5) % COUNT) / COUNT); // shuffled even heights: a gap between neighbours at load
        gsap.to(el, { x: gsap.utils.random(-30, 30), duration: gsap.utils.random(2.5, 4), ease: "sine.inOut",
          yoyo: true, repeat: -1 });
      });
    }, root);
    return () => ctx.revert();
  }, []);
  return (
    <div className="falling-nuts" ref={root} aria-hidden="true">
      {Array.from({ length: COUNT }, (_, i) => <img key={i} className="nut" src="/logo.svg" alt="" />)}
    </div>
  );
}
