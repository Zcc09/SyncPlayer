// Minimal JSON for the mpv IPC, the config file and the GitHub release check.
//
// Hand-written on purpose: the Python build had no third-party runtime and the app
// ships next to mpv rather than in a package manager, so pulling in a JSON library
// would be the only dependency in the whole project.
#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace sp {

class Json {
 public:
  enum class Type { Null, Bool, Number, String, Array, Object };

  Json() = default;
  static Json null() { return Json(); }
  static Json boolean(bool v);
  static Json number(double v);
  static Json integer(int64_t v);
  static Json str(std::string v);
  static Json array();
  static Json object();

  Type type() const { return type_; }
  bool is_null() const { return type_ == Type::Null; }
  bool is_bool() const { return type_ == Type::Bool; }
  bool is_number() const { return type_ == Type::Number; }
  bool is_string() const { return type_ == Type::String; }
  bool is_array() const { return type_ == Type::Array; }
  bool is_object() const { return type_ == Type::Object; }

  // Accessors are forgiving: mpv returns null for properties that do not exist yet
  // and the app must not throw because a value was momentarily absent.
  bool as_bool(bool fallback = false) const;
  double as_double(double fallback = 0.0) const;
  int64_t as_int(int64_t fallback = 0) const;
  const std::string& as_string() const;
  std::string str_or(std::string fallback) const;

  // Object access
  bool has(std::string_view key) const;
  const Json& operator[](std::string_view key) const;
  Json& operator[](std::string_view key);
  void set(std::string key, Json value);
  void erase(std::string_view key);
  std::vector<std::string> keys() const;

  // Array access
  size_t size() const;
  const Json& at(size_t i) const;
  Json& at(size_t i);
  void push_back(Json v);

  // Numbers may arrive as strings from some mpv properties and as numbers from
  // others; these read either.
  double numeric(double fallback = 0.0) const;

  std::string dump(bool pretty = false) const;

 private:
  Type type_ = Type::Null;
  bool bool_ = false;
  double number_ = 0.0;
  std::string string_;
  std::vector<Json> array_;
  std::map<std::string, Json> object_;
};

// Returns null Json and fills *error on failure. Never throws.
Json json_parse(std::string_view text, std::string* error = nullptr);

}  // namespace sp
