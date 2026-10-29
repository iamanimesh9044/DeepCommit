# Grade one step from the RSpec JSON reports that test.sh wrote to /logs/verifier/{f2p,p2p}.json.
#
# Writes /logs/verifier/reward.json (numbers only, as Harbor requires):
#   reward      1 if every fail-to-pass and every pass-to-pass example passed, else 0
#   f2p_total, f2p_passed, p2p_total, p2p_passed
# and /logs/verifier/ctrf.json with one entry per example.
#
#   ruby grade.rb                 grade the reports
#   ruby grade.rb --error "why"   record a verifier error (reward 0, no reports)
require 'json'

LOG = '/logs/verifier'.freeze

def load_report(name)
  path = File.join(LOG, "#{name}.json")
  return nil unless File.exist?(path) && File.size(path).positive?

  JSON.parse(File.read(path))
rescue JSON::ParserError
  nil
end

def summarize(name, report, expected)
  return { ok: !expected, total: 0, passed: 0, examples: [], why: expected ? "#{name}: no report (load error?)" : nil } if report.nil?

  examples = report.fetch('examples', [])
  summary = report.fetch('summary', {})
  failed = examples.count { |e| e['status'] == 'failed' }
  passed = examples.count { |e| e['status'] != 'failed' }
  outside = summary.fetch('errors_outside_of_examples_count', 0)
  why = []
  why << "#{name}: #{failed} failed" if failed.positive?
  why << "#{name}: #{outside} error(s) outside examples" if outside.positive?
  why << "#{name}: no examples ran" if expected && examples.empty?
  { ok: why.empty?, total: examples.size, passed: passed, examples: examples, why: why.empty? ? nil : why.join('; ') }
end

def ctrf(groups)
  tests = groups.flat_map do |group, result|
    result[:examples].map do |e|
      {
        name: e['full_description'], status: e['status'] == 'failed' ? 'failed' : (e['status'] == 'pending' ? 'skipped' : 'passed'),
        duration: ((e['run_time'] || 0) * 1000).round, suite: group, filePath: e['file_path'], line: e['line_number'],
        message: e.dig('exception', 'message')
      }.compact
    end
  end
  counts = tests.group_by { |t| t[:status] }.transform_values(&:size)
  { results: { tool: { name: 'rspec' },
               summary: { tests: tests.size, passed: counts.fetch('passed', 0), failed: counts.fetch('failed', 0),
                          skipped: counts.fetch('skipped', 0), pending: 0, other: 0 },
               tests: tests } }
end

def write(rewards, groups, notes)
  File.write(File.join(LOG, 'reward.json'), JSON.generate(rewards))
  File.write(File.join(LOG, 'ctrf.json'), JSON.pretty_generate(ctrf(groups)))
  puts "csf-grade: #{rewards.to_json}"
  notes.compact.each { |n| puts "csf-grade: #{n}" }
end

if ARGV[0] == '--error'
  write({ reward: 0, f2p_total: 0, f2p_passed: 0, p2p_total: 0, p2p_passed: 0, verifier_error: 1 }, {}, [ARGV[1]])
  exit 0
end

f2p = summarize('F2P', load_report('f2p'), true)
p2p_expected = File.exist?(File.join(LOG, 'p2p.log'))
p2p = summarize('P2P', load_report('p2p'), p2p_expected)
reward = f2p[:ok] && p2p[:ok] ? 1 : 0
write({ reward: reward, f2p_total: f2p[:total], f2p_passed: f2p[:passed], p2p_total: p2p[:total], p2p_passed: p2p[:passed] },
      { 'f2p' => f2p, 'p2p' => p2p }, [f2p[:why], p2p[:why]])
