//! Stability gate. Mirrors Python `state.py`.
//!
//! Two responsibilities:
//!   1. Require N consecutive identical frames before letting a decision
//!      through. Filters out mid-render flicker.
//!   2. Suppress a re-press of the same prompt within a cooldown window.
//!
//! Pure logic, no I/O. Time can be injected for tests.

use std::collections::{HashMap, VecDeque};
use std::time::{Duration, Instant};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GateDecision {
    pub proceed: bool,
    pub reason: String,
}

impl GateDecision {
    fn ok(reason: impl Into<String>) -> Self {
        Self { proceed: true, reason: reason.into() }
    }
    fn block(reason: impl Into<String>) -> Self {
        Self { proceed: false, reason: reason.into() }
    }
}

pub struct PromptStateTracker {
    stable_required: usize,
    cooldown: Duration,
    recent: VecDeque<String>,
    acted: HashMap<String, Instant>,
}

impl PromptStateTracker {
    pub fn new(stable_frames: usize, cooldown: Duration) -> Self {
        let stable_required = stable_frames.max(1);
        Self {
            stable_required,
            cooldown,
            recent: VecDeque::with_capacity(stable_required),
            acted: HashMap::new(),
        }
    }

    /// Pass `None` when no actionable prompt is on screen — resets the streak.
    pub fn observe(&mut self, prompt_hash: Option<&str>) -> GateDecision {
        self.observe_at(prompt_hash, Instant::now())
    }

    pub fn observe_at(&mut self, prompt_hash: Option<&str>, now: Instant) -> GateDecision {
        self.gc(now);

        let hash = match prompt_hash {
            None => {
                self.recent.clear();
                return GateDecision::block("no actionable prompt");
            }
            Some(h) => h,
        };

        if self.recent.len() == self.stable_required {
            self.recent.pop_front();
        }
        self.recent.push_back(hash.to_string());

        if self.recent.len() < self.stable_required {
            return GateDecision::block(format!(
                "need {} stable frames (have {})",
                self.stable_required,
                self.recent.len()
            ));
        }
        if self.recent.iter().any(|h| h != hash) {
            return GateDecision::block("frame not yet stable");
        }
        if let Some(last) = self.acted.get(hash) {
            let elapsed = now.duration_since(*last);
            if elapsed < self.cooldown {
                let left = self.cooldown - elapsed;
                return GateDecision::block(format!(
                    "cooldown ({:.1}s left)",
                    left.as_secs_f64()
                ));
            }
        }
        GateDecision::ok("stable")
    }

    pub fn mark_acted(&mut self, prompt_hash: &str) {
        self.mark_acted_at(prompt_hash, Instant::now());
    }

    pub fn mark_acted_at(&mut self, prompt_hash: &str, now: Instant) {
        self.acted.insert(prompt_hash.to_string(), now);
    }

    fn gc(&mut self, now: Instant) {
        // Expire entries older than 5x cooldown to keep memory bounded.
        let cutoff = self.cooldown.checked_mul(5).unwrap_or(self.cooldown);
        self.acted.retain(|_, t| now.duration_since(*t) < cutoff);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_observation_blocked_until_stable() {
        let mut s = PromptStateTracker::new(2, Duration::from_secs(15));
        assert!(!s.observe(Some("abc")).proceed);
        assert!(s.observe(Some("abc")).proceed);
    }

    #[test]
    fn frame_change_resets_streak() {
        let mut s = PromptStateTracker::new(2, Duration::from_secs(15));
        assert!(!s.observe(Some("abc")).proceed);
        assert!(!s.observe(Some("xyz")).proceed); // streak broken
        assert!(s.observe(Some("xyz")).proceed);  // new streak completes
    }

    #[test]
    fn no_prompt_clears_streak() {
        let mut s = PromptStateTracker::new(2, Duration::from_secs(15));
        assert!(!s.observe(Some("abc")).proceed);
        assert!(!s.observe(None).proceed);
        // After clear, need 2 again.
        assert!(!s.observe(Some("abc")).proceed);
        assert!(s.observe(Some("abc")).proceed);
    }

    #[test]
    fn cooldown_blocks_immediate_re_action() {
        let mut s = PromptStateTracker::new(1, Duration::from_secs(15));
        let t0 = Instant::now();
        assert!(s.observe_at(Some("abc"), t0).proceed);
        s.mark_acted_at("abc", t0);
        let t1 = t0 + Duration::from_secs(5);
        let d = s.observe_at(Some("abc"), t1);
        assert!(!d.proceed);
        assert!(d.reason.contains("cooldown"));
    }

    #[test]
    fn cooldown_expires() {
        let mut s = PromptStateTracker::new(1, Duration::from_secs(15));
        let t0 = Instant::now();
        s.observe_at(Some("abc"), t0);
        s.mark_acted_at("abc", t0);
        let t2 = t0 + Duration::from_secs(16);
        assert!(s.observe_at(Some("abc"), t2).proceed);
    }
}
