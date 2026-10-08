#!/usr/bin/env bash
# Build the Rails app that agent-workload.py's "rails" workload copies for
# every run: rails new (minimal, PostgreSQL), three models with a foreign
# key, a jsonb column, unique and composite indexes, fixtures and model
# tests (transactions, savepoints, constraint errors, jsonb queries), plus a
# second migration that the workload applies mid-run (schema change).
#
#   rails-app-setup.sh <dir>        (needs ruby, bundler, rails, pg gems)
set -euo pipefail
DIR=${1:?usage: rails-app-setup.sh <dir>}
rm -rf "$DIR"
rails new "$DIR" --minimal --database=postgresql --skip-git --skip-docker --skip-ci --skip-kamal \
  --skip-solid --skip-thruster --skip-rubocop --skip-brakeman --skip-bundle --quiet
cd "$DIR"
# gems go beside the app (shared by every copy the workload makes); the
# absolute path is recorded in the app's .bundle/config
BUNDLE=$(command -v bundle || command -v bundle3.3)   # Ubuntu's ruby-full ships bundle3.3
"$BUNDLE" config set --local path "$(cd .. && pwd)/bundle"
"$BUNDLE" install --quiet
cat > config/database.yml <<'YML'
default: &default
  adapter: postgresql
  encoding: unicode
  pool: 5
  url: <%= ENV.fetch("DATABASE_URL", "postgres://localhost/setup_only") %>
development:
  <<: *default
test:
  <<: *default
YML

bin/rails g model User email:string:uniq name:string --no-test-framework -q
bin/rails g model Project user:references name:string settings:jsonb archived:boolean --no-test-framework -q
bin/rails g model Task project:references title:string done:boolean position:integer --no-test-framework -q
sed -i 's/t.jsonb :settings/t.jsonb :settings, null: false, default: {}/; s/t.boolean :archived/t.boolean :archived, null: false, default: false/' db/migrate/*_create_projects.rb
sed -i 's/t.boolean :done/t.boolean :done, null: false, default: false/' db/migrate/*_create_tasks.rb
# indexes after create_table, before the end of def change
sed -i '0,/^  end$/s//    add_index :tasks, [:project_id, :position], unique: true\n  end/' db/migrate/*_create_tasks.rb
sed -i '0,/^  end$/s//    add_index :projects, :settings, using: :gin\n  end/' db/migrate/*_create_projects.rb

cat > app/models/user.rb <<'RB'
class User < ApplicationRecord
  has_many :projects, dependent: :destroy
  validates :email, presence: true, uniqueness: true
end
RB
cat > app/models/project.rb <<'RB'
class Project < ApplicationRecord
  belongs_to :user
  has_many :tasks, -> { order(:position) }, dependent: :delete_all
  scope :tier, ->(t) { where("settings->>'tier' = ?", t) }
end
RB
cat > app/models/task.rb <<'RB'
class Task < ApplicationRecord
  belongs_to :project
end
RB

mkdir -p test/fixtures test/models
cat > test/fixtures/users.yml <<'YML'
<% 1.upto(20) do |i| %>
user_<%= i %>:
  email: user<%= i %>@example.com
  name: User <%= i %>
<% end %>
YML
cat > test/fixtures/projects.yml <<'YML'
<% 1.upto(20) do |i| %>
project_<%= i %>:
  user: user_<%= i %>
  name: Project <%= i %>
  settings: <%= { tier: i.even? ? "pro" : "free", seats: i }.to_json %>
<% end %>
YML
cat > test/fixtures/tasks.yml <<'YML'
<% 1.upto(20) do |p| %><% 1.upto(10) do |i| %>
task_<%= p %>_<%= i %>:
  project: project_<%= p %>
  title: Task <%= i %>
  position: <%= i %>
  done: <%= i % 3 == 0 %>
<% end %><% end %>
YML

cat > test/models/workload_test.rb <<'RB'
require "test_helper"

class WorkloadTest < ActiveSupport::TestCase
  test "fixtures loaded" do
    assert_equal 20, User.count
    assert_equal 200, Task.count
  end

  test "jsonb query" do
    assert_equal 10, Project.tier("pro").count
    assert_equal 20, Project.where("(settings->>'seats')::int > 0").count
  end

  test "joins and aggregates" do
    open_by_user = Task.joins(project: :user).where(done: false).group("users.email").count
    assert_equal 20, open_by_user.size
  end

  test "unique violation inside a transaction" do
    assert_raises(ActiveRecord::RecordNotUnique) do
      User.transaction { User.new(email: "user1@example.com").save!(validate: false) }
    end
    assert_equal 20, User.count
  end

  test "foreign key violation" do
    # skip the belongs_to validation so the database's FK is what refuses it
    assert_raises(ActiveRecord::InvalidForeignKey) { Task.new(project_id: -1, title: "x", position: 1).save!(validate: false) }
  end

  test "nested transaction rolls back to savepoint" do
    User.transaction do
      User.create!(email: "outer@example.com")
      User.transaction(requires_new: true) do
        User.create!(email: "inner@example.com")
        raise ActiveRecord::Rollback
      end
    end
    assert User.exists?(email: "outer@example.com")
    assert_not User.exists?(email: "inner@example.com")
  end

  test "bulk insert and update" do
    p = projects(:project_1)
    Task.insert_all((11..510).map { |i| { project_id: p.id, title: "bulk #{i}", position: i, done: false } })
    Task.where(project: p).where("position > 10").update_all(done: true)
    assert_equal 500, Task.where(project: p, done: true).where("position > 10").count
  end

  test "cascade delete" do
    u = users(:user_2)
    assert_difference -> { Task.count }, -10 do
      u.destroy!
    end
  end
end
RB

# The schema change the workload applies after the first test run.
rm -rf ../rails-change && mkdir -p ../rails-change
# stamped a minute after the generated migrations (Rails rejects timestamps
# in the future)
CHANGE_TS=$(date -u -d '+1 min' +%Y%m%d%H%M%S)
cat > "../rails-change/${CHANGE_TS}_add_due_at_and_priority.rb" <<'RB'
class AddDueAtAndPriority < ActiveRecord::Migration[ActiveRecord::Migration.current_version]
  def change
    add_column :tasks, :due_at, :datetime
    add_column :tasks, :priority, :integer, null: false, default: 0
    add_index :tasks, [:project_id, :priority]
    add_column :projects, :slug, :string
    add_index :projects, :slug, unique: true
  end
end
RB
cat > ../rails-change/schema_change_test.rb <<'RB'
require "test_helper"

class SchemaChangeTest < ActiveSupport::TestCase
  test "new columns are usable" do
    t = tasks(:task_1_1)
    t.update!(priority: 5, due_at: Time.current)
    assert_equal 1, Task.where(priority: 5).count
    projects(:project_1).update!(slug: "p1")
    assert_raises(ActiveRecord::RecordNotUnique) { projects(:project_2).update!(slug: "p1") }
  end
end
RB
echo "rails app ready in $DIR"
