export default function AudioHistory({ history = [], score }) {
const percent = value => value == null ? "—" : Math.round(value * 100) + "%";
return (              <div className="chart-card">
                <div className="section-heading">
                  <h3>Spoof probability</h3>
                  <strong>{percent(score?.spoof_probability)}</strong>
                </div>
                <div className="chart">
                  <div className="chart-labels">
                    <span>100%</span>
                    <span>50%</span>
                    <span>0%</span>
                  </div>
                  <svg
                    viewBox="0 0 500 150"
                    preserveAspectRatio="none"
                    role="img"
                    aria-label="Spoof probability over recent analysis windows"
                  >
                    <path
                      className="chart-grid"
                      d="M0 1H500M0 75H500M0 149H500M100 0V150M200 0V150M300 0V150M400 0V150"
                    />
                    {history.length > 0 && (
                      <polyline
                        className="chart-line"
                        points={history
                          .map(
                            (value, index) =>
                              `${history.length === 1 ? 250 : (index / (history.length - 1)) * 500},${149 - value * 148}`,
                          )
                          .join(" ")}
                      />
                    )}
                    {history.length === 1 && (
                      <circle
                        cx="250"
                        cy={149 - history[0] * 148}
                        r="3"
                        fill="#8666cd"
                      />
                    )}
                  </svg>
                  {!history.length && (
                    <span className="chart-empty">
                      Waiting for the first analysis window
                    </span>
                  )}
                </div>
                <div className="chart-footer">
                  <span>Recent analysis windows</span>
                  <span>
                    {score?.inference_ms != null
                      ? `${Math.round(score.inference_ms)} ms inference`
                      : "No score yet"}
                  </span>
                </div>
              </div>);
}
