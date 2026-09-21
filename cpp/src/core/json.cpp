#include "core/json.h"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace sp {
namespace {

const Json kNull;

bool is_space(char c) {
  return c == ' ' || c == '\t' || c == '\n' || c == '\r';
}

void append_utf8(std::string& out, uint32_t cp) {
  if (cp <= 0x7F) {
    out.push_back(static_cast<char>(cp));
  } else if (cp <= 0x7FF) {
    out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
    out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
  } else if (cp <= 0xFFFF) {
    out.push_back(static_cast<char>(0xE0 | (cp >> 12)));
    out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
    out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
  } else {
    out.push_back(static_cast<char>(0xF0 | (cp >> 18)));
    out.push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
    out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
    out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
  }
}

void dump_string(std::string& out, const std::string& s) {
  out.push_back('"');
  for (size_t i = 0; i < s.size(); ++i) {
    unsigned char c = static_cast<unsigned char>(s[i]);
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      case '\b': out += "\\b"; break;
      case '\f': out += "\\f"; break;
      default:
        if (c < 0x20) {
          char buf[8];
          std::snprintf(buf, sizeof buf, "\\u%04x", c);
          out += buf;
        } else {
          out.push_back(static_cast<char>(c));
        }
    }
  }
  out.push_back('"');
}

void dump_number(std::string& out, double v) {
  if (std::isnan(v) || std::isinf(v)) {
    out += "0";  // JSON has no NaN/Infinity; mpv sends null for absent values
    return;
  }
  if (v == std::floor(v) && std::fabs(v) < 1e15) {
    char buf[32];
    std::snprintf(buf, sizeof buf, "%lld", static_cast<long long>(v));
    out += buf;
    return;
  }
  char buf[40];
  std::snprintf(buf, sizeof buf, "%.10g", v);
  out += buf;
}

// ---------------------------------------------------------------------------
// parser
// ---------------------------------------------------------------------------
class Parser {
 public:
  Parser(std::string_view text, std::string* error) : s_(text), err_(error) {}

  Json parse() {
    skip();
    Json v = value(0);
    if (failed_) return Json();
    skip();
    if (i_ != s_.size()) {
      fail("trailing characters after the JSON value");
      return Json();
    }
    return v;
  }

 private:
  std::string_view s_;
  size_t i_ = 0;
  std::string* err_ = nullptr;
  bool failed_ = false;

  void fail(const std::string& why) {
    if (!failed_) {
      failed_ = true;
      if (err_) {
        *err_ = why + " at offset " + std::to_string(i_);
      }
    }
  }

  void skip() {
    while (i_ < s_.size() && is_space(s_[i_])) ++i_;
  }

  bool consume(char c) {
    if (i_ < s_.size() && s_[i_] == c) {
      ++i_;
      return true;
    }
    return false;
  }

  bool consume_word(std::string_view w) {
    if (s_.compare(i_, w.size(), w) == 0) {
      i_ += w.size();
      return true;
    }
    return false;
  }

  Json value(int depth) {
    if (depth > 200) {
      fail("nesting too deep");
      return Json();
    }
    skip();
    if (i_ >= s_.size()) {
      fail("unexpected end of input");
      return Json();
    }
    char c = s_[i_];
    if (c == '{') return object(depth);
    if (c == '[') return array(depth);
    if (c == '"') return Json::str(string());
    if (consume_word("true")) return Json::boolean(true);
    if (consume_word("false")) return Json::boolean(false);
    if (consume_word("null")) return Json::null();
    if (c == '-' || (c >= '0' && c <= '9')) return number();
    fail(std::string("unexpected character '") + c + "'");
    return Json();
  }

  Json object(int depth) {
    Json o = Json::object();
    ++i_;  // {
    skip();
    if (consume('}')) return o;
    while (true) {
      skip();
      if (i_ >= s_.size() || s_[i_] != '"') {
        fail("expected a key string");
        return Json();
      }
      std::string key = string();
      skip();
      if (!consume(':')) {
        fail("expected ':' after a key");
        return Json();
      }
      Json v = value(depth + 1);
      if (failed_) return Json();
      o.set(std::move(key), std::move(v));
      skip();
      if (consume(',')) continue;
      if (consume('}')) return o;
      fail("expected ',' or '}'");
      return Json();
    }
  }

  Json array(int depth) {
    Json a = Json::array();
    ++i_;  // [
    skip();
    if (consume(']')) return a;
    while (true) {
      Json v = value(depth + 1);
      if (failed_) return Json();
      a.push_back(std::move(v));
      skip();
      if (consume(',')) continue;
      if (consume(']')) return a;
      fail("expected ',' or ']'");
      return Json();
    }
  }

  Json number() {
    size_t start = i_;
    if (consume('-')) {}
    while (i_ < s_.size() && s_[i_] >= '0' && s_[i_] <= '9') ++i_;
    if (i_ < s_.size() && s_[i_] == '.') {
      ++i_;
      while (i_ < s_.size() && s_[i_] >= '0' && s_[i_] <= '9') ++i_;
    }
    if (i_ < s_.size() && (s_[i_] == 'e' || s_[i_] == 'E')) {
      ++i_;
      if (i_ < s_.size() && (s_[i_] == '+' || s_[i_] == '-')) ++i_;
      while (i_ < s_.size() && s_[i_] >= '0' && s_[i_] <= '9') ++i_;
    }
    std::string text(s_.substr(start, i_ - start));
    if (text.empty() || text == "-") {
      fail("bad number");
      return Json();
    }
    return Json::number(std::strtod(text.c_str(), nullptr));
  }

  uint32_t hex4() {
    uint32_t v = 0;
    for (int k = 0; k < 4; ++k) {
      if (i_ >= s_.size()) {
        fail("truncated \\u escape");
        return 0;
      }
      char c = s_[i_++];
      v <<= 4;
      if (c >= '0' && c <= '9') v |= static_cast<uint32_t>(c - '0');
      else if (c >= 'a' && c <= 'f') v |= static_cast<uint32_t>(c - 'a' + 10);
      else if (c >= 'A' && c <= 'F') v |= static_cast<uint32_t>(c - 'A' + 10);
      else { fail("bad hex digit in \\u escape"); return 0; }
    }
    return v;
  }

  std::string string() {
    std::string out;
    ++i_;  // opening quote
    while (i_ < s_.size()) {
      unsigned char c = static_cast<unsigned char>(s_[i_++]);
      if (c == '"') return out;
      if (c == '\\') {
        if (i_ >= s_.size()) break;
        char e = s_[i_++];
        switch (e) {
          case '"': out.push_back('"'); break;
          case '\\': out.push_back('\\'); break;
          case '/': out.push_back('/'); break;
          case 'b': out.push_back('\b'); break;
          case 'f': out.push_back('\f'); break;
          case 'n': out.push_back('\n'); break;
          case 'r': out.push_back('\r'); break;
          case 't': out.push_back('\t'); break;
          case 'u': {
            uint32_t cp = hex4();
            if (failed_) return out;
            if (cp >= 0xD800 && cp <= 0xDBFF && i_ + 1 < s_.size() &&
                s_[i_] == '\\' && s_[i_ + 1] == 'u') {
              i_ += 2;
              uint32_t lo = hex4();
              if (failed_) return out;
              cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
            }
            append_utf8(out, cp);
            break;
          }
          default:
            fail("unknown escape");
            return out;
        }
        continue;
      }
      if (c < 0x20) {
        fail("control character in a string");
        return out;
      }
      out.push_back(static_cast<char>(c));
    }
    fail("unterminated string");
    return out;
  }
};

void dump_to(std::string& out, const Json& v, bool pretty, int indent) {
  auto newline = [&](int level) {
    if (!pretty) return;
    out.push_back('\n');
    out.append(static_cast<size_t>(level) * 2, ' ');
  };
  switch (v.type()) {
    case Json::Type::Null: out += "null"; break;
    case Json::Type::Bool: out += v.as_bool() ? "true" : "false"; break;
    case Json::Type::Number: dump_number(out, v.as_double()); break;
    case Json::Type::String: dump_string(out, v.as_string()); break;
    case Json::Type::Array: {
      if (v.size() == 0) { out += "[]"; break; }
      out.push_back('[');
      for (size_t i = 0; i < v.size(); ++i) {
        if (i) out.push_back(',');
        newline(indent + 1);
        dump_to(out, v.at(i), pretty, indent + 1);
      }
      newline(indent);
      out.push_back(']');
      break;
    }
    case Json::Type::Object: {
      auto keys = v.keys();
      if (keys.empty()) { out += "{}"; break; }
      out.push_back('{');
      bool first = true;
      for (const auto& k : keys) {
        if (!first) out.push_back(',');
        first = false;
        newline(indent + 1);
        dump_string(out, k);
        out.push_back(':');
        if (pretty) out.push_back(' ');
        dump_to(out, v[k], pretty, indent + 1);
      }
      newline(indent);
      out.push_back('}');
      break;
    }
  }
}

}  // namespace

// ---------------------------------------------------------------------------
Json Json::boolean(bool v) {
  Json j;
  j.type_ = Type::Bool;
  j.bool_ = v;
  return j;
}
Json Json::number(double v) {
  Json j;
  j.type_ = Type::Number;
  j.number_ = v;
  return j;
}
Json Json::integer(int64_t v) { return number(static_cast<double>(v)); }
Json Json::str(std::string v) {
  Json j;
  j.type_ = Type::String;
  j.string_ = std::move(v);
  return j;
}
Json Json::array() {
  Json j;
  j.type_ = Type::Array;
  return j;
}
Json Json::object() {
  Json j;
  j.type_ = Type::Object;
  return j;
}

bool Json::as_bool(bool fallback) const {
  if (type_ == Type::Bool) return bool_;
  if (type_ == Type::Number) return number_ != 0.0;
  if (type_ == Type::String) return string_ == "true" || string_ == "yes";
  return fallback;
}

double Json::as_double(double fallback) const {
  if (type_ == Type::Number) return number_;
  if (type_ == Type::String) {
    try {
      return std::stod(string_);
    } catch (...) {
      return fallback;
    }
  }
  if (type_ == Type::Bool) return bool_ ? 1.0 : 0.0;
  return fallback;
}

int64_t Json::as_int(int64_t fallback) const {
  if (type_ == Type::Number) return static_cast<int64_t>(number_);
  if (type_ == Type::String) {
    try {
      return static_cast<int64_t>(std::stoll(string_));
    } catch (...) {
      return fallback;
    }
  }
  return fallback;
}

const std::string& Json::as_string() const {
  static const std::string empty;
  return type_ == Type::String ? string_ : empty;
}

std::string Json::str_or(std::string fallback) const {
  if (type_ == Type::String) return string_;
  if (type_ == Type::Number) {
    std::string out;
    dump_number(out, number_);
    return out;
  }
  if (type_ == Type::Bool) return bool_ ? "true" : "false";
  return fallback;
}

double Json::numeric(double fallback) const {
  if (type_ == Type::Number) return number_;
  return as_double(fallback);
}

bool Json::has(std::string_view key) const {
  return type_ == Type::Object && object_.find(std::string(key)) != object_.end();
}

const Json& Json::operator[](std::string_view key) const {
  if (type_ != Type::Object) return kNull;
  auto it = object_.find(std::string(key));
  return it == object_.end() ? kNull : it->second;
}

Json& Json::operator[](std::string_view key) {
  if (type_ != Type::Object) {
    type_ = Type::Object;
    object_.clear();
  }
  return object_[std::string(key)];
}

void Json::set(std::string key, Json value) {
  if (type_ != Type::Object) {
    type_ = Type::Object;
    object_.clear();
  }
  object_[std::move(key)] = std::move(value);
}

void Json::erase(std::string_view key) {
  if (type_ == Type::Object) object_.erase(std::string(key));
}

std::vector<std::string> Json::keys() const {
  std::vector<std::string> out;
  if (type_ == Type::Object) {
    out.reserve(object_.size());
    for (const auto& kv : object_) out.push_back(kv.first);
  }
  return out;
}

size_t Json::size() const {
  if (type_ == Type::Array) return array_.size();
  if (type_ == Type::Object) return object_.size();
  return 0;
}

const Json& Json::at(size_t i) const {
  if (type_ != Type::Array || i >= array_.size()) return kNull;
  return array_[i];
}

Json& Json::at(size_t i) {
  if (type_ != Type::Array) {
    type_ = Type::Array;
    array_.clear();
  }
  if (i >= array_.size()) array_.resize(i + 1);
  return array_[i];
}

void Json::push_back(Json v) {
  if (type_ != Type::Array) {
    type_ = Type::Array;
    array_.clear();
  }
  array_.push_back(std::move(v));
}

std::string Json::dump(bool pretty) const {
  std::string out;
  dump_to(out, *this, pretty, 0);
  return out;
}

Json json_parse(std::string_view text, std::string* error) {
  Parser p(text, error);
  return p.parse();
}

}  // namespace sp
